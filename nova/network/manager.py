# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Network Hosts are responsible for allocating ips and setting up network.

There are multiple backend drivers that handle specific types of networking
topologies.  All of the network commands are issued to a subclass of
:class:`NetworkManager`.

**Related Flags**

:network_driver:  Driver to use for network creation
:flat_network_bridge:  Bridge device for simple network instances
:flat_interface:  FlatDhcp will bridge into this interface if set
:flat_network_dns:  Dns for simple network
:vlan_start:  First VLAN for private networks
:vpn_ip:  Public IP for the cloudpipe VPN servers
:vpn_start:  First Vpn port for private networks
:cnt_vpn_clients:  Number of addresses reserved for vpn clients
:network_size:  Number of addresses in each private subnet
:floating_range:  Floating IP address block
:fixed_range:  Fixed IP address block
:fixed_ip_disassociate_timeout:  Seconds after which a deallocated ip
                                 is disassociated
:create_unique_mac_address_attempts:  Number of times to attempt creating
                                      a unique mac address

"""

import datetime
import functools
import itertools
import math
import re
import socket
import uuid

from eventlet import greenpool
import netaddr

from nova.compute import api as compute_api
from nova import context
from nova import exception
from nova import ipv6
from nova import manager
from nova.network import api as network_api
from nova.network import model as network_model
from nova.network import rpcapi as network_rpcapi
from nova.openstack.common import cfg
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.openstack.common.notifier import api as notifier
from nova.openstack.common import timeutils
from nova.openstack.common import uuidutils
import nova.policy
from nova import quota
from nova import servicegroup
from nova import utils


LOG = logging.getLogger(__name__)

QUOTAS = quota.QUOTAS

network_opts = [
    cfg.StrOpt('flat_network_bridge',
               default=None,
               help='Bridge for simple network instances'),
    cfg.StrOpt('flat_network_dns',
               default='8.8.4.4',
               help='Dns for simple network'),
    cfg.BoolOpt('flat_injected',
                default=False,
                help='Whether to attempt to inject network setup into guest'),
    cfg.StrOpt('flat_interface',
               default=None,
               help='FlatDhcp will bridge into this interface if set'),
    cfg.IntOpt('vlan_start',
               default=100,
               help='First VLAN for private networks'),
    cfg.StrOpt('vlan_interface',
               default=None,
               help='vlans will bridge into this interface if set'),
    cfg.IntOpt('num_networks',
               default=1,
               help='Number of networks to support'),
    cfg.StrOpt('vpn_ip',
               default='$my_ip',
               help='Public IP for the cloudpipe VPN servers'),
    cfg.IntOpt('vpn_start',
               default=1000,
               help='First Vpn port for private networks'),
    cfg.BoolOpt('multi_host',
                default=False,
                help='Default value for multi_host in networks'),
    cfg.IntOpt('network_size',
               default=256,
               help='Number of addresses in each private subnet'),
    cfg.StrOpt('floating_range',
               default='4.4.4.0/24',
               help='Floating IP address block'),
    cfg.StrOpt('default_floating_pool',
               default='nova',
               help='Default pool for floating ips'),
    cfg.StrOpt('fixed_range',
               default='10.0.0.0/8',
               help='Fixed IP address block'),
    cfg.StrOpt('fixed_range_v6',
               default='fd00::/48',
               help='Fixed IPv6 address block'),
    cfg.StrOpt('gateway',
               default=None,
               help='Default IPv4 gateway'),
    cfg.StrOpt('gateway_v6',
               default=None,
               help='Default IPv6 gateway'),
    cfg.IntOpt('cnt_vpn_clients',
               default=0,
               help='Number of addresses reserved for vpn clients'),
    cfg.IntOpt('fixed_ip_disassociate_timeout',
               default=600,
               help='Seconds after which a deallocated ip is disassociated'),
    cfg.IntOpt('create_unique_mac_address_attempts',
               default=5,
               help='Number of attempts to create unique mac address'),
    cfg.BoolOpt('auto_assign_floating_ip',
                default=False,
                help='Autoassigning floating ip to VM'),
    cfg.StrOpt('network_host',
               default=socket.getfqdn(),
               help='Network host to use for ip allocation in flat modes'),
    cfg.BoolOpt('fake_call',
                default=False,
                help='If True, skip using the queue and make local calls'),
    cfg.BoolOpt('force_dhcp_release',
                default=False,
                help='If True, send a dhcp release on instance termination'),
    cfg.BoolOpt('share_dhcp_address',
                default=False,
                help='If True in multi_host mode, all compute hosts share '
                     'the same dhcp address.'),
    cfg.BoolOpt('update_dns_entries',
                default=False,
                help='If True, when a DNS entry must be updated, it sends a '
                     'fanout cast to all network hosts to update their DNS '
                     'entries in multi host mode'),
    cfg.StrOpt('dhcp_domain',
               default='novalocal',
               help='domain to use for building the hostnames'),
    cfg.StrOpt('l3_lib',
               default='nova.network.l3.LinuxNetL3',
               help="Indicates underlying L3 management library")
    ]

CONF = cfg.CONF
CONF.register_opts(network_opts)
CONF.import_opt('fake_network', 'nova.config')
CONF.import_opt('floating_ip_dns_manager', 'nova.config')
CONF.import_opt('instance_dns_domain', 'nova.config')
CONF.import_opt('instance_dns_manager', 'nova.config')
CONF.import_opt('network_driver', 'nova.config')
CONF.import_opt('use_ipv6', 'nova.config')
CONF.import_opt('my_ip', 'nova.config')


class RPCAllocateFixedIP(object):
    """Mixin class originally for FlatDCHP and VLAN network managers.

    used since they share code to RPC.call allocate_fixed_ip on the
    correct network host to configure dnsmasq
    """

    servicegroup_api = None

    def _allocate_fixed_ips(self, context, instance_id, host, networks,
                            **kwargs):
        """Calls allocate_fixed_ip once for each network."""
        green_pool = greenpool.GreenPool()

        vpn = kwargs.get('vpn')
        requested_networks = kwargs.get('requested_networks')

        for network in networks:
            address = None
            if requested_networks is not None:
                for address in (fixed_ip for (uuid, fixed_ip) in
                                requested_networks if network['uuid'] == uuid):
                    break

            # NOTE(vish): if we are not multi_host pass to the network host
            # NOTE(tr3buchet): but if we are, host came from instance['host']
            if not network['multi_host']:
                host = network['host']
            # NOTE(vish): if there is no network host, set one
            if host is None:
                host = self.network_rpcapi.set_network_host(context, network)
            if host != self.host:
                # need to call allocate_fixed_ip to correct network host
                green_pool.spawn_n(self.network_rpcapi._rpc_allocate_fixed_ip,
                        context, instance_id, network['id'], address, vpn,
                        host)
            else:
                # i am the correct host, run here
                self.allocate_fixed_ip(context, instance_id, network,
                                       vpn=vpn, address=address)

        # wait for all of the allocates (if any) to finish
        green_pool.waitall()

    def _rpc_allocate_fixed_ip(self, context, instance_id, network_id,
                               **kwargs):
        """Sits in between _allocate_fixed_ips and allocate_fixed_ip to
        perform network lookup on the far side of rpc.
        """
        network = self._get_network_by_id(context, network_id)
        return self.allocate_fixed_ip(context, instance_id, network, **kwargs)

    def deallocate_fixed_ip(self, context, address, host=None, teardown=True):
        """Call the superclass deallocate_fixed_ip if i'm the correct host
        otherwise call to the correct host"""
        fixed_ip = self.db.fixed_ip_get_by_address(context, address)
        network = self._get_network_by_id(context, fixed_ip['network_id'])

        # NOTE(vish): if we are not multi_host pass to the network host
        # NOTE(tr3buchet): but if we are, host came from instance['host']
        if not network['multi_host']:
            host = network['host']
        if host == self.host:
            # NOTE(vish): deallocate the fixed ip locally
            return super(RPCAllocateFixedIP, self).deallocate_fixed_ip(context,
                    address)

        if network['multi_host']:
            service = self.db.service_get_by_host_and_topic(context,
                                                            host,
                                                            'network')
            if not service or not self.servicegroup_api.service_is_up(service):
                # NOTE(vish): deallocate the fixed ip locally but don't
                #             teardown network devices
                return super(RPCAllocateFixedIP, self).deallocate_fixed_ip(
                        context, address, teardown=False)

        self.network_rpcapi.deallocate_fixed_ip(context, address, host)


def wrap_check_policy(func):
    """Check policy corresponding to the wrapped methods prior to execution"""

    @functools.wraps(func)
    def wrapped(self, context, *args, **kwargs):
        action = func.__name__
        check_policy(context, action)
        return func(self, context, *args, **kwargs)

    return wrapped


def check_policy(context, action):
    target = {
        'project_id': context.project_id,
        'user_id': context.user_id,
    }
    _action = 'network:%s' % action
    nova.policy.enforce(context, _action, target)


class FloatingIP(object):
    """Mixin class for adding floating IP functionality to a manager."""

    servicegroup_api = None

    def init_host_floating_ips(self):
        """Configures floating ips owned by host."""

        admin_context = context.get_admin_context()
        try:
            floating_ips = self.db.floating_ip_get_all_by_host(admin_context,
                                                               self.host)
        except exception.NotFound:
            return

        for floating_ip in floating_ips:
            fixed_ip_id = floating_ip.get('fixed_ip_id')
            if fixed_ip_id:
                try:
                    fixed_ip_ref = self.db.fixed_ip_get(admin_context,
                                                        fixed_ip_id)
                except exception.FixedIpNotFound:
                    msg = _('Fixed ip %(fixed_ip_id)s not found') % locals()
                    LOG.debug(msg)
                    continue
                fixed_address = fixed_ip_ref['address']
                interface = CONF.public_interface or floating_ip['interface']
                try:
                    self.l3driver.add_floating_ip(floating_ip['address'],
                            fixed_address, interface)
                except exception.ProcessExecutionError:
                    LOG.debug(_('Interface %(interface)s not found'), locals())
                    raise exception.NoFloatingIpInterface(interface=interface)

    @wrap_check_policy
    def allocate_for_instance(self, context, **kwargs):
        """Handles allocating the floating IP resources for an instance.

        calls super class allocate_for_instance() as well

        rpc.called by network_api
        """
        instance_id = kwargs.get('instance_id')
        instance_uuid = kwargs.get('instance_uuid')
        project_id = kwargs.get('project_id')
        requested_networks = kwargs.get('requested_networks')
        LOG.debug(_("floating IP allocation for instance |%s|"),
                  instance_uuid=instance_uuid, context=context)
        # call the next inherited class's allocate_for_instance()
        # which is currently the NetworkManager version
        # do this first so fixed ip is already allocated
        nw_info = super(FloatingIP, self).allocate_for_instance(context,
                                                                **kwargs)
        if CONF.auto_assign_floating_ip:
            # allocate a floating ip
            floating_address = self.allocate_floating_ip(context, project_id,
                True)
            # set auto_assigned column to true for the floating ip
            self.db.floating_ip_set_auto_assigned(context, floating_address)

            # get the first fixed address belonging to the instance
            fixed_ips = nw_info.fixed_ips()
            fixed_address = fixed_ips[0]['address']

            # associate the floating ip to fixed_ip
            self.associate_floating_ip(context,
                                       floating_address,
                                       fixed_address,
                                       affect_auto_assigned=True)

            # create a fresh set of network info that contains the floating ip
            nw_info = self.get_instance_nw_info(context, **kwargs)

        return nw_info

    @wrap_check_policy
    def deallocate_for_instance(self, context, **kwargs):
        """Handles deallocating floating IP resources for an instance.

        calls super class deallocate_for_instance() as well.

        rpc.called by network_api
        """
        instance_id = kwargs.get('instance_id')

        # NOTE(francois.charlier): in some cases the instance might be
        # deleted before the IPs are released, so we need to get deleted
        # instances too
        instance = self.db.instance_get(
                context.elevated(read_deleted='yes'), instance_id)

        try:
            fixed_ips = self.db.fixed_ip_get_by_instance(context,
                                                         instance['uuid'])
        except exception.FixedIpNotFoundForInstance:
            fixed_ips = []
        # add to kwargs so we can pass to super to save a db lookup there
        kwargs['fixed_ips'] = fixed_ips
        for fixed_ip in fixed_ips:
            fixed_id = fixed_ip['id']
            floating_ips = self.db.floating_ip_get_by_fixed_ip_id(context,
                                                                  fixed_id)
            # disassociate floating ips related to fixed_ip
            for floating_ip in floating_ips:
                address = floating_ip['address']
                try:
                    self.disassociate_floating_ip(context,
                                                  address,
                                                  affect_auto_assigned=True)
                except exception.FloatingIpNotAssociated:
                    LOG.exception(_("Floating IP is not associated. Ignore."))
                # deallocate if auto_assigned
                if floating_ip['auto_assigned']:
                    self.deallocate_floating_ip(context, address,
                                                affect_auto_assigned=True)

        # call the next inherited class's deallocate_for_instance()
        # which is currently the NetworkManager version
        # call this after so floating IPs are handled first
        super(FloatingIP, self).deallocate_for_instance(context, **kwargs)

    def _floating_ip_owned_by_project(self, context, floating_ip):
        """Raises if floating ip does not belong to project"""
        if context.is_admin:
            return

        if floating_ip['project_id'] != context.project_id:
            if floating_ip['project_id'] is None:
                LOG.warn(_('Address |%(address)s| is not allocated'),
                           {'address': floating_ip['address']})
                raise exception.NotAuthorized()
            else:
                LOG.warn(_('Address |%(address)s| is not allocated to your '
                           'project |%(project)s|'),
                           {'address': floating_ip['address'],
                           'project': context.project_id})
                raise exception.NotAuthorized()

    @wrap_check_policy
    def allocate_floating_ip(self, context, project_id, auto_assigned=False,
                             pool=None):
        """Gets a floating ip from the pool."""
        # NOTE(tr3buchet): all network hosts in zone now use the same pool
        pool = pool or CONF.default_floating_pool
        use_quota = not auto_assigned

        # Check the quota; can't put this in the API because we get
        # called into from other places
        try:
            if use_quota:
                reservations = QUOTAS.reserve(context, floating_ips=1)
        except exception.OverQuota:
            pid = context.project_id
            LOG.warn(_("Quota exceeded for %(pid)s, tried to allocate "
                       "floating IP") % locals())
            raise exception.FloatingIpLimitExceeded()

        try:
            floating_ip = self.db.floating_ip_allocate_address(context,
                                                               project_id,
                                                               pool)
            payload = dict(project_id=project_id, floating_ip=floating_ip)
            notifier.notify(context,
                            notifier.publisher_id("network"),
                            'network.floating_ip.allocate',
                            notifier.INFO, payload)

            # Commit the reservations
            if use_quota:
                QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                if use_quota:
                    QUOTAS.rollback(context, reservations)

        return floating_ip

    @wrap_check_policy
    def deallocate_floating_ip(self, context, address,
                               affect_auto_assigned=False):
        """Returns an floating ip to the pool."""
        floating_ip = self.db.floating_ip_get_by_address(context, address)

        # handle auto_assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            return
        use_quota = not floating_ip.get('auto_assigned')

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # make sure floating ip is not associated
        if floating_ip['fixed_ip_id']:
            floating_address = floating_ip['address']
            raise exception.FloatingIpAssociated(address=floating_address)

        # clean up any associated DNS entries
        self._delete_all_entries_for_ip(context,
                                       floating_ip['address'])
        payload = dict(project_id=floating_ip['project_id'],
                       floating_ip=floating_ip['address'])
        notifier.notify(context,
                        notifier.publisher_id("network"),
                        'network.floating_ip.deallocate',
                        notifier.INFO, payload=payload)

        # Get reservations...
        try:
            if use_quota:
                reservations = QUOTAS.reserve(context, floating_ips=-1)
            else:
                reservations = None
        except Exception:
            reservations = None
            LOG.exception(_("Failed to update usages deallocating "
                            "floating IP"))

        self.db.floating_ip_deallocate(context, address)

        # Commit the reservations
        if reservations:
            QUOTAS.commit(context, reservations)

    @wrap_check_policy
    def associate_floating_ip(self, context, floating_address, fixed_address,
                              affect_auto_assigned=False):
        """Associates a floating ip with a fixed ip.

        Makes sure everything makes sense then calls _associate_floating_ip,
        rpc'ing to correct host if i'm not it.
        """
        floating_ip = self.db.floating_ip_get_by_address(context,
                                                         floating_address)
        # handle auto_assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            return

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # disassociate any already associated
        orig_instance_uuid = None
        if floating_ip['fixed_ip_id']:
            # find previously associated instance
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'])
            orig_instance_uuid = fixed_ip['instance_uuid']

            self.disassociate_floating_ip(context, floating_address)

        fixed_ip = self.db.fixed_ip_get_by_address(context, fixed_address)

        # send to correct host, unless i'm the correct host
        network = self._get_network_by_id(context.elevated(),
                                          fixed_ip['network_id'])
        if network['multi_host']:
            instance = self.db.instance_get_by_uuid(context,
                                                    fixed_ip['instance_uuid'])
            host = instance['host']
        else:
            host = network['host']

        interface = CONF.public_interface or floating_ip['interface']
        if host == self.host:
            # i'm the correct host
            self._associate_floating_ip(context, floating_address,
                                        fixed_address, interface,
                                        fixed_ip['instance_uuid'])
        else:
            # send to correct host
            self.network_rpcapi._associate_floating_ip(context,
                    floating_address, fixed_address, interface, host)

        return orig_instance_uuid

    def _associate_floating_ip(self, context, floating_address, fixed_address,
                               interface, instance_uuid):
        """Performs db and driver calls to associate floating ip & fixed ip"""
        # associate floating ip
        self.db.floating_ip_fixed_ip_associate(context,
                                               floating_address,
                                               fixed_address,
                                               self.host)
        try:
            # gogo driver time
            self.l3driver.add_floating_ip(floating_address, fixed_address,
                    interface)
        except exception.ProcessExecutionError as e:
            fixed_address = self.db.floating_ip_disassociate(context,
                                                             floating_address)
            if "Cannot find device" in str(e):
                LOG.error(_('Interface %(interface)s not found'), locals())
                raise exception.NoFloatingIpInterface(interface=interface)

        payload = dict(project_id=context.project_id,
                       instance_id=instance_uuid,
                       floating_ip=floating_address)
        notifier.notify(context,
                        notifier.publisher_id("network"),
                        'network.floating_ip.associate',
                        notifier.INFO, payload=payload)

    @wrap_check_policy
    def disassociate_floating_ip(self, context, address,
                                 affect_auto_assigned=False):
        """Disassociates a floating ip from its fixed ip.

        Makes sure everything makes sense then calls _disassociate_floating_ip,
        rpc'ing to correct host if i'm not it.
        """
        floating_ip = self.db.floating_ip_get_by_address(context, address)

        # handle auto assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            raise exception.CannotDisassociateAutoAssignedFloatingIP()

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # make sure floating ip is associated
        if not floating_ip.get('fixed_ip_id'):
            floating_address = floating_ip['address']
            raise exception.FloatingIpNotAssociated(address=floating_address)

        fixed_ip = self.db.fixed_ip_get(context, floating_ip['fixed_ip_id'])

        # send to correct host, unless i'm the correct host
        network = self._get_network_by_id(context, fixed_ip['network_id'])
        interface = CONF.public_interface or floating_ip['interface']
        if network['multi_host']:
            instance = self.db.instance_get_by_uuid(context,
                                                    fixed_ip['instance_uuid'])
            service = self.db.service_get_by_host_and_topic(
                    context.elevated(), instance['host'], 'network')
            if service and self.servicegroup_api.service_is_up(service):
                host = instance['host']
            else:
                # NOTE(vish): if the service is down just deallocate the data
                #             locally. Set the host to local so the call will
                #             not go over rpc and set interface to None so the
                #             teardown in the driver does not happen.
                host = self.host
                interface = None
        else:
            host = network['host']

        if host == self.host:
            # i'm the correct host
            self._disassociate_floating_ip(context, address, interface,
                                           fixed_ip['instance_uuid'])
        else:
            # send to correct host
            self.network_rpcapi._disassociate_floating_ip(context, address,
                    interface, host)

    def _disassociate_floating_ip(self, context, address, interface,
                                  instance_uuid):
        """Performs db and driver calls to disassociate floating ip"""
        # disassociate floating ip
        fixed_address = self.db.floating_ip_disassociate(context, address)

        if interface:
            # go go driver time
            self.l3driver.remove_floating_ip(address, fixed_address, interface)
        payload = dict(project_id=context.project_id,
                       instance_id=instance_uuid,
                       floating_ip=address)
        notifier.notify(context,
                        notifier.publisher_id("network"),
                        'network.floating_ip.disassociate',
                        notifier.INFO, payload=payload)

    @wrap_check_policy
    def get_floating_ip(self, context, id):
        """Returns a floating IP as a dict"""
        return dict(self.db.floating_ip_get(context, id).iteritems())

    @wrap_check_policy
    def get_floating_pools(self, context):
        """Returns list of floating pools"""
        pools = self.db.floating_ip_get_pools(context)
        return [dict(pool.iteritems()) for pool in pools]

    @wrap_check_policy
    def get_floating_ip_by_address(self, context, address):
        """Returns a floating IP as a dict"""
        return dict(self.db.floating_ip_get_by_address(context,
                                                       address).iteritems())

    @wrap_check_policy
    def get_floating_ips_by_project(self, context):
        """Returns the floating IPs allocated to a project"""
        ips = self.db.floating_ip_get_all_by_project(context,
                                                     context.project_id)
        return [dict(ip.iteritems()) for ip in ips]

    @wrap_check_policy
    def get_floating_ips_by_fixed_address(self, context, fixed_address):
        """Returns the floating IPs associated with a fixed_address"""
        floating_ips = self.db.floating_ip_get_by_fixed_address(context,
                                                                fixed_address)
        return [floating_ip['address'] for floating_ip in floating_ips]

    def _is_stale_floating_ip_address(self, context, floating_ip):
        try:
            self._floating_ip_owned_by_project(context, floating_ip)
        except exception.NotAuthorized:
            return True
        return False if floating_ip.get('fixed_ip_id') else True

    @wrap_check_policy
    def migrate_instance_start(self, context, instance_uuid,
                               floating_addresses,
                               rxtx_factor=None, project_id=None,
                               source=None, dest=None):
        # We only care if floating_addresses are provided and we're
        # switching hosts
        if not floating_addresses or (source and source == dest):
            return

        LOG.info(_("Starting migration network for instance"
                   " %(instance_uuid)s"), locals())
        for address in floating_addresses:
            floating_ip = self.db.floating_ip_get_by_address(context,
                                                             address)

            if self._is_stale_floating_ip_address(context, floating_ip):
                LOG.warn(_("Floating ip address |%(address)s| no longer "
                           "belongs to instance %(instance_uuid)s. Will not"
                           "migrate it "), locals())
                continue

            interface = CONF.public_interface or floating_ip['interface']
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'])
            self.l3driver.remove_floating_ip(floating_ip['address'],
                                             fixed_ip['address'],
                                             interface)

            # NOTE(wenjianhn): Make this address will not be bound to public
            # interface when restarts nova-network on dest compute node
            self.db.floating_ip_update(context,
                                       floating_ip['address'],
                                       {'host': None})

    @wrap_check_policy
    def migrate_instance_finish(self, context, instance_uuid,
                                floating_addresses, host=None,
                                rxtx_factor=None, project_id=None,
                                source=None, dest=None):
        # We only care if floating_addresses are provided and we're
        # switching hosts
        if host and not dest:
            dest = host
        if not floating_addresses or (source and source == dest):
            return

        LOG.info(_("Finishing migration network for instance"
                   " %(instance_uuid)s"), locals())

        for address in floating_addresses:
            floating_ip = self.db.floating_ip_get_by_address(context,
                                                             address)

            if self._is_stale_floating_ip_address(context, floating_ip):
                LOG.warn(_("Floating ip address |%(address)s| no longer "
                           "belongs to instance %(instance_uuid)s. Will not"
                           "setup it."), locals())
                continue

            self.db.floating_ip_update(context,
                                       floating_ip['address'],
                                       {'host': dest})

            interface = CONF.public_interface or floating_ip['interface']
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'])
            self.l3driver.add_floating_ip(floating_ip['address'],
                                          fixed_ip['address'],
                                          interface)

    def _prepare_domain_entry(self, context, domain):
        domainref = self.db.dnsdomain_get(context, domain)
        scope = domainref.scope
        if scope == 'private':
            av_zone = domainref.availability_zone
            this_domain = {'domain': domain,
                         'scope': scope,
                         'availability_zone': av_zone}
        else:
            project = domainref.project_id
            this_domain = {'domain': domain,
                         'scope': scope,
                         'project': project}
        return this_domain

    @wrap_check_policy
    def get_dns_domains(self, context):
        domains = []

        db_domain_list = self.db.dnsdomain_list(context)
        floating_driver_domain_list = self.floating_dns_manager.get_domains()
        instance_driver_domain_list = self.instance_dns_manager.get_domains()

        for db_domain in db_domain_list:
            if (db_domain in floating_driver_domain_list or
                db_domain in instance_driver_domain_list):
                    domain_entry = self._prepare_domain_entry(context,
                                                              db_domain)
                    if domain_entry:
                        domains.append(domain_entry)
            else:
                LOG.warn(_('Database inconsistency: DNS domain |%s| is '
                         'registered in the Nova db but not visible to '
                         'either the floating or instance DNS driver. It '
                         'will be ignored.'), db_domain)

        return domains

    @wrap_check_policy
    def add_dns_entry(self, context, address, name, dns_type, domain):
        self.floating_dns_manager.create_entry(name, address,
                                               dns_type, domain)

    @wrap_check_policy
    def modify_dns_entry(self, context, address, name, domain):
        self.floating_dns_manager.modify_address(name, address,
                                                 domain)

    @wrap_check_policy
    def delete_dns_entry(self, context, name, domain):
        self.floating_dns_manager.delete_entry(name, domain)

    def _delete_all_entries_for_ip(self, context, address):
        domain_list = self.get_dns_domains(context)
        for domain in domain_list:
            names = self.get_dns_entries_by_address(context,
                                                    address,
                                                    domain['domain'])
            for name in names:
                self.delete_dns_entry(context, name, domain['domain'])

    @wrap_check_policy
    def get_dns_entries_by_address(self, context, address, domain):
        return self.floating_dns_manager.get_entries_by_address(address,
                                                                domain)

    @wrap_check_policy
    def get_dns_entries_by_name(self, context, name, domain):
        return self.floating_dns_manager.get_entries_by_name(name,
                                                             domain)

    @wrap_check_policy
    def create_private_dns_domain(self, context, domain, av_zone):
        self.db.dnsdomain_register_for_zone(context, domain, av_zone)
        try:
            self.instance_dns_manager.create_domain(domain)
        except exception.FloatingIpDNSExists:
            LOG.warn(_('Domain |%(domain)s| already exists, '
                       'changing zone to |%(av_zone)s|.'),
                     {'domain': domain, 'av_zone': av_zone})

    @wrap_check_policy
    def create_public_dns_domain(self, context, domain, project):
        self.db.dnsdomain_register_for_project(context, domain, project)
        try:
            self.floating_dns_manager.create_domain(domain)
        except exception.FloatingIpDNSExists:
            LOG.warn(_('Domain |%(domain)s| already exists, '
                       'changing project to |%(project)s|.'),
                     {'domain': domain, 'project': project})

    @wrap_check_policy
    def delete_dns_domain(self, context, domain):
        self.db.dnsdomain_unregister(context, domain)
        self.floating_dns_manager.delete_domain(domain)

    def _get_project_for_domain(self, context, domain):
        return self.db.dnsdomain_project(context, domain)


class NetworkManager(manager.SchedulerDependentManager):
    """Implements common network manager functionality.

    This class must be subclassed to support specific topologies.

    host management:
        hosts configure themselves for networks they are assigned to in the
        table upon startup. If there are networks in the table which do not
        have hosts, those will be filled in and have hosts configured
        as the hosts pick them up one at time during their periodic task.
        The one at a time part is to flatten the layout to help scale
    """

    RPC_API_VERSION = '1.3'

    # If True, this manager requires VIF to create a bridge.
    SHOULD_CREATE_BRIDGE = False

    # If True, this manager requires VIF to create VLAN tag.
    SHOULD_CREATE_VLAN = False

    # if True, this manager leverages DHCP
    DHCP = False

    timeout_fixed_ips = True

    required_create_args = []

    def __init__(self, network_driver=None, *args, **kwargs):
        if not network_driver:
            network_driver = CONF.network_driver
        self.driver = importutils.import_module(network_driver)
        self.instance_dns_manager = importutils.import_object(
                CONF.instance_dns_manager)
        self.instance_dns_domain = CONF.instance_dns_domain
        self.floating_dns_manager = importutils.import_object(
                CONF.floating_ip_dns_manager)
        self.network_api = network_api.API()
        self.network_rpcapi = network_rpcapi.NetworkAPI()
        self.security_group_api = compute_api.SecurityGroupAPI()
        self.compute_api = compute_api.API(
                                   security_group_api=self.security_group_api)
        self.servicegroup_api = servicegroup.API()

        # NOTE(tr3buchet: unless manager subclassing NetworkManager has
        #                 already imported ipam, import nova ipam here
        if not hasattr(self, 'ipam'):
            self._import_ipam_lib('nova.network.nova_ipam_lib')
        l3_lib = kwargs.get("l3_lib", CONF.l3_lib)
        self.l3driver = importutils.import_object(l3_lib)

        super(NetworkManager, self).__init__(service_name='network',
                                                *args, **kwargs)

    def _import_ipam_lib(self, ipam_lib):
        self.ipam = importutils.import_module(ipam_lib).get_ipam_lib(self)

    @lockutils.synchronized('get_dhcp', 'nova-')
    def _get_dhcp_ip(self, context, network_ref, host=None):
        """Get the proper dhcp address to listen on."""
        # NOTE(vish): this is for compatibility
        if not network_ref.get('multi_host') or CONF.share_dhcp_address:
            return network_ref['gateway']

        if not host:
            host = self.host
        network_id = network_ref['id']
        try:
            fip = self.db.fixed_ip_get_by_network_host(context,
                                                       network_id,
                                                       host)
            return fip['address']
        except exception.FixedIpNotFoundForNetworkHost:
            elevated = context.elevated()
            return self.db.fixed_ip_associate_pool(elevated,
                                                   network_id,
                                                   host=host)

    def get_dhcp_leases(self, ctxt, network_ref):
        """Broker the request to the driver to fetch the dhcp leases"""
        return self.driver.get_dhcp_leases(ctxt, network_ref)

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """
        # NOTE(vish): Set up networks for which this host already has
        #             an ip address.
        ctxt = context.get_admin_context()
        for network in self.db.network_get_all_by_host(ctxt, self.host):
            self._setup_network_on_host(ctxt, network)
            if CONF.update_dns_entries:
                dev = self.driver.get_dev(network)
                self.driver.update_dns(ctxt, dev, network)

    @manager.periodic_task
    def _disassociate_stale_fixed_ips(self, context):
        if self.timeout_fixed_ips:
            now = timeutils.utcnow()
            timeout = CONF.fixed_ip_disassociate_timeout
            time = now - datetime.timedelta(seconds=timeout)
            num = self.db.fixed_ip_disassociate_all_by_timeout(context,
                                                               self.host,
                                                               time)
            if num:
                LOG.debug(_('Disassociated %s stale fixed ip(s)'), num)

    def set_network_host(self, context, network_ref):
        """Safely sets the host of the network."""
        LOG.debug(_('setting network host'), context=context)
        host = self.db.network_set_host(context,
                                        network_ref['id'],
                                        self.host)
        return host

    def _do_trigger_security_group_members_refresh_for_instance(self,
                                                                instance_id):
        # NOTE(francois.charlier): the instance may have been deleted already
        # thus enabling `read_deleted`
        admin_context = context.get_admin_context(read_deleted='yes')
        if uuidutils.is_uuid_like(instance_id):
            instance_ref = self.db.instance_get_by_uuid(admin_context,
                                                        instance_id)
        else:
            instance_ref = self.db.instance_get(admin_context, instance_id)

        groups = instance_ref['security_groups']
        group_ids = [group['id'] for group in groups]

        self.security_group_api.trigger_members_refresh(admin_context,
                                                        group_ids)
        self.security_group_api.trigger_handler('security_group_members',
                                                admin_context, group_ids)

    def _do_trigger_security_group_handler(self, handler, instance_id):
        admin_context = context.get_admin_context(read_deleted="yes")
        if uuidutils.is_uuid_like(instance_id):
            instance_ref = self.db.instance_get_by_uuid(admin_context,
                                                        instance_id)
        else:
            instance_ref = self.db.instance_get(admin_context,
                                                instance_id)
        for group_name in [group['name'] for group
                in instance_ref['security_groups']]:
            self.security_group_api.trigger_handler(handler, admin_context,
                    instance_ref, group_name)

    def get_floating_ips_by_fixed_address(self, context, fixed_address):
        # NOTE(jkoelker) This is just a stub function. Managers supporting
        #                floating ips MUST override this or use the Mixin
        return []

    @wrap_check_policy
    def get_instance_uuids_by_ip_filter(self, context, filters):
        fixed_ip_filter = filters.get('fixed_ip')
        ip_filter = re.compile(str(filters.get('ip')))
        ipv6_filter = re.compile(str(filters.get('ip6')))

        # NOTE(jkoelker) Should probably figure out a better way to do
        #                this. But for now it "works", this could suck on
        #                large installs.

        vifs = self.db.virtual_interface_get_all(context)
        results = []

        for vif in vifs:
            if vif['instance_uuid'] is None:
                continue

            network = self._get_network_by_id(context, vif['network_id'])
            fixed_ipv6 = None
            if network['cidr_v6'] is not None:
                fixed_ipv6 = ipv6.to_global(network['cidr_v6'],
                                            vif['address'],
                                            context.project_id)

            if fixed_ipv6 and ipv6_filter.match(fixed_ipv6):
                results.append({'instance_uuid': vif['instance_uuid'],
                                'ip': fixed_ipv6})

            vif_id = vif['id']
            fixed_ips = self.db.fixed_ips_by_virtual_interface(context,
                                                               vif_id)
            for fixed_ip in fixed_ips:
                if not fixed_ip or not fixed_ip['address']:
                    continue
                if fixed_ip['address'] == fixed_ip_filter:
                    results.append({'instance_uuid': vif['instance_uuid'],
                                    'ip': fixed_ip['address']})
                    continue
                if ip_filter.match(fixed_ip['address']):
                    results.append({'instance_uuid': vif['instance_uuid'],
                                    'ip': fixed_ip['address']})
                    continue
                for floating_ip in fixed_ip.get('floating_ips', []):
                    if not floating_ip or not floating_ip['address']:
                        continue
                    if ip_filter.match(floating_ip['address']):
                        results.append({'instance_uuid': vif['instance_uuid'],
                                        'ip': floating_ip['address']})
                        continue

        return results

    def _get_networks_for_instance(self, context, instance_id, project_id,
                                   requested_networks=None):
        """Determine & return which networks an instance should connect to."""
        # TODO(tr3buchet) maybe this needs to be updated in the future if
        #                 there is a better way to determine which networks
        #                 a non-vlan instance should connect to
        if requested_networks is not None and len(requested_networks) != 0:
            network_uuids = [uuid for (uuid, fixed_ip) in requested_networks]
            networks = self._get_networks_by_uuids(context, network_uuids)
        else:
            try:
                networks = self.db.network_get_all(context)
            except exception.NoNetworksFound:
                return []
        # return only networks which are not vlan networks
        return [network for network in networks if
                not network['vlan']]

    @wrap_check_policy
    def allocate_for_instance(self, context, **kwargs):
        """Handles allocating the various network resources for an instance.

        rpc.called by network_api
        """
        instance_id = kwargs['instance_id']
        instance_uuid = kwargs['instance_uuid']
        host = kwargs['host']
        project_id = kwargs['project_id']
        rxtx_factor = kwargs['rxtx_factor']
        requested_networks = kwargs.get('requested_networks')
        vpn = kwargs['vpn']
        admin_context = context.elevated()
        LOG.debug(_("network allocations"), instance_uuid=instance_uuid,
                  context=context)
        networks = self._get_networks_for_instance(admin_context,
                                        instance_id, project_id,
                                        requested_networks=requested_networks)
        LOG.debug(_('networks retrieved for instance: |%(networks)s|'),
                  locals(), context=context, instance_uuid=instance_uuid)
        self._allocate_mac_addresses(context, instance_uuid, networks)
        self._allocate_fixed_ips(admin_context, instance_id,
                                 host, networks, vpn=vpn,
                                 requested_networks=requested_networks)

        if CONF.update_dns_entries:
            network_ids = [network['id'] for network in networks]
            self.network_rpcapi.update_dns(context, network_ids)

        return self.get_instance_nw_info(context, instance_id, instance_uuid,
                                         rxtx_factor, host)

    @wrap_check_policy
    def deallocate_for_instance(self, context, **kwargs):
        """Handles deallocating various network resources for an instance.

        rpc.called by network_api
        kwargs can contain fixed_ips to circumvent another db lookup
        """
        # NOTE(francois.charlier): in some cases the instance might be
        # deleted before the IPs are released, so we need to get deleted
        # instances too
        read_deleted_context = context.elevated(read_deleted='yes')

        instance_id = kwargs.pop('instance_id')
        instance = self.db.instance_get(read_deleted_context, instance_id)
        host = kwargs.get('host')

        try:
            fixed_ips = (kwargs.get('fixed_ips') or
                         self.db.fixed_ip_get_by_instance(read_deleted_context,
                                                          instance['uuid']))
        except exception.FixedIpNotFoundForInstance:
            fixed_ips = []
        LOG.debug(_("network deallocation for instance"), instance=instance,
                  context=read_deleted_context)
        # deallocate fixed ips
        for fixed_ip in fixed_ips:
            self.deallocate_fixed_ip(context, fixed_ip['address'], host=host)

        if CONF.update_dns_entries:
            network_ids = [fixed_ip['network_id'] for fixed_ip in fixed_ips]
            self.network_rpcapi.update_dns(context, network_ids)

        # deallocate vifs (mac addresses)
        self.db.virtual_interface_delete_by_instance(read_deleted_context,
                                                     instance['uuid'])

    @wrap_check_policy
    def get_instance_nw_info(self, context, instance_id, instance_uuid,
                                            rxtx_factor, host, **kwargs):
        """Creates network info list for instance.

        called by allocate_for_instance and network_api
        context needs to be elevated
        :returns: network info list [(network,info),(network,info)...]
        where network = dict containing pertinent data from a network db object
        and info = dict containing pertinent networking data
        """
        vifs = self.db.virtual_interface_get_by_instance(context,
                                                         instance_uuid)
        networks = {}

        for vif in vifs:
            if vif.get('network_id') is not None:
                network = self._get_network_by_id(context, vif['network_id'])
                networks[vif['uuid']] = network

        nw_info = self.build_network_info_model(context, vifs, networks,
                                                         rxtx_factor, host)
        return nw_info

    def build_network_info_model(self, context, vifs, networks,
                                 rxtx_factor, instance_host):
        """Builds a NetworkInfo object containing all network information
        for an instance"""
        nw_info = network_model.NetworkInfo()
        for vif in vifs:
            vif_dict = {'id': vif['uuid'],
                        'address': vif['address']}

            # handle case where vif doesn't have a network
            if not networks.get(vif['uuid']):
                vif = network_model.VIF(**vif_dict)
                nw_info.append(vif)
                continue

            # get network dict for vif from args and build the subnets
            network = networks[vif['uuid']]
            subnets = self._get_subnets_from_network(context, network, vif,
                                                     instance_host)

            # if rxtx_cap data are not set everywhere, set to none
            try:
                rxtx_cap = network['rxtx_base'] * rxtx_factor
            except (TypeError, KeyError):
                rxtx_cap = None

            # get fixed_ips
            v4_IPs = self.ipam.get_v4_ips_by_interface(context,
                                                       network['uuid'],
                                                       vif['uuid'],
                                                       network['project_id'])
            v6_IPs = self.ipam.get_v6_ips_by_interface(context,
                                                       network['uuid'],
                                                       vif['uuid'],
                                                       network['project_id'])

            # create model FixedIPs from these fixed_ips
            network_IPs = [network_model.FixedIP(address=ip_address)
                           for ip_address in v4_IPs + v6_IPs]

            # get floating_ips for each fixed_ip
            # add them to the fixed ip
            for fixed_ip in network_IPs:
                if fixed_ip['version'] == 6:
                    continue
                gfipbfa = self.ipam.get_floating_ips_by_fixed_address
                floating_ips = gfipbfa(context, fixed_ip['address'])
                floating_ips = [network_model.IP(address=ip['address'],
                                                 type='floating')
                                for ip in floating_ips]
                for ip in floating_ips:
                    fixed_ip.add_floating_ip(ip)

            # add ips to subnets they belong to
            for subnet in subnets:
                subnet['ips'] = [fixed_ip for fixed_ip in network_IPs
                                 if fixed_ip.is_in_subnet(subnet)]

            # convert network into a Network model object
            network = network_model.Network(**self._get_network_dict(network))

            # since network currently has no subnets, easily add them all
            network['subnets'] = subnets

            # add network and rxtx cap to vif_dict
            vif_dict['network'] = network
            if rxtx_cap:
                vif_dict['rxtx_cap'] = rxtx_cap

            # create the vif model and add to network_info
            vif = network_model.VIF(**vif_dict)
            nw_info.append(vif)

        return nw_info

    def _get_network_dict(self, network):
        """Returns the dict representing necessary and meta network fields"""
        # get generic network fields
        network_dict = {'id': network['uuid'],
                        'bridge': network['bridge'],
                        'label': network['label'],
                        'tenant_id': network['project_id']}

        # get extra information
        if network.get('injected'):
            network_dict['injected'] = network['injected']

        return network_dict

    def _get_subnets_from_network(self, context, network,
                                  vif, instance_host=None):
        """Returns the 1 or 2 possible subnets for a nova network"""
        # get subnets
        ipam_subnets = self.ipam.get_subnets_by_net_id(context,
                           network['project_id'], network['uuid'], vif['uuid'])

        subnets = []
        for subnet in ipam_subnets:
            subnet_dict = {'cidr': subnet['cidr'],
                           'gateway': network_model.IP(
                                             address=subnet['gateway'],
                                             type='gateway')}
            # deal with dhcp
            if self.DHCP:
                if network.get('multi_host'):
                    dhcp_server = self._get_dhcp_ip(context, network,
                                                    instance_host)
                else:
                    dhcp_server = self._get_dhcp_ip(context, subnet)
                subnet_dict['dhcp_server'] = dhcp_server

            subnet_object = network_model.Subnet(**subnet_dict)

            # add dns info
            for k in ['dns1', 'dns2']:
                if subnet.get(k):
                    subnet_object.add_dns(
                         network_model.IP(address=subnet[k], type='dns'))

            # get the routes for this subnet
            # NOTE(tr3buchet): default route comes from subnet gateway
            if subnet.get('id'):
                routes = self.ipam.get_routes_by_ip_block(context,
                                         subnet['id'], network['project_id'])
                for route in routes:
                    cidr = netaddr.IPNetwork('%s/%s' % (route['destination'],
                                                        route['netmask'])).cidr
                    subnet_object.add_route(
                            network_model.Route(cidr=str(cidr),
                                                gateway=network_model.IP(
                                                    address=route['gateway'],
                                                    type='gateway')))

            subnets.append(subnet_object)

        return subnets

    def _allocate_mac_addresses(self, context, instance_uuid, networks):
        """Generates mac addresses and creates vif rows in db for them."""
        for network in networks:
            self.add_virtual_interface(context, instance_uuid, network['id'])

    def add_virtual_interface(self, context, instance_uuid, network_id):
        vif = {'address': utils.generate_mac_address(),
               'instance_uuid': instance_uuid,
               'network_id': network_id,
               'uuid': str(uuid.uuid4())}
        # try FLAG times to create a vif record with a unique mac_address
        for i in xrange(CONF.create_unique_mac_address_attempts):
            try:
                return self.db.virtual_interface_create(context, vif)
            except exception.VirtualInterfaceCreateException:
                vif['address'] = utils.generate_mac_address()
        else:
            self.db.virtual_interface_delete_by_instance(context,
                                                         instance_uuid)
            raise exception.VirtualInterfaceMacAddressException()

    @wrap_check_policy
    def add_fixed_ip_to_instance(self, context, instance_id, host, network_id):
        """Adds a fixed ip to an instance from specified network."""
        if uuidutils.is_uuid_like(network_id):
            network = self.get_network(context, network_id)
        else:
            network = self._get_network_by_id(context, network_id)
        self._allocate_fixed_ips(context, instance_id, host, [network])

    def get_backdoor_port(self, context):
        """Return backdoor port for eventlet_backdoor"""
        return self.backdoor_port

    @wrap_check_policy
    def remove_fixed_ip_from_instance(self, context, instance_id, host,
                                      address):
        """Removes a fixed ip from an instance from specified network."""
        fixed_ips = self.db.fixed_ip_get_by_instance(context, instance_id)
        for fixed_ip in fixed_ips:
            if fixed_ip['address'] == address:
                self.deallocate_fixed_ip(context, address, host)
                return
        raise exception.FixedIpNotFoundForSpecificInstance(
                                    instance_id=instance_id, ip=address)

    def _validate_instance_zone_for_dns_domain(self, context, instance):
        instance_zone = instance.get('availability_zone')
        if not self.instance_dns_domain:
            return True

        instance_domain = self.instance_dns_domain
        domainref = self.db.dnsdomain_get(context, instance_zone)
        dns_zone = domainref.availability_zone
        if dns_zone and (dns_zone != instance_zone):
            LOG.warn(_('instance-dns-zone is |%(domain)s|, '
                       'which is in availability zone |%(zone)s|. '
                       'Instance is in zone |%(zone2)s|. '
                       'No DNS record will be created.'),
                     {'domain': instance_domain,
                      'zone': dns_zone,
                      'zone2': instance_zone},
                     instance=instance)
            return False
        else:
            return True

    def allocate_fixed_ip(self, context, instance_id, network, **kwargs):
        """Gets a fixed ip from the pool."""
        # TODO(vish): when this is called by compute, we can associate compute
        #             with a network, or a cluster of computes with a network
        #             and use that network here with a method like
        #             network_get_by_compute_host
        address = None
        instance_ref = self.db.instance_get(context, instance_id)

        if network['cidr']:
            address = kwargs.get('address', None)
            if address:
                address = self.db.fixed_ip_associate(context,
                                                     address,
                                                     instance_ref['uuid'],
                                                     network['id'])
            else:
                address = self.db.fixed_ip_associate_pool(context.elevated(),
                                                          network['id'],
                                                          instance_ref['uuid'])
            self._do_trigger_security_group_members_refresh_for_instance(
                                                                   instance_id)
            self._do_trigger_security_group_handler(
                'instance_add_security_group', instance_id)
            get_vif = self.db.virtual_interface_get_by_instance_and_network
            vif = get_vif(context, instance_ref['uuid'], network['id'])
            values = {'allocated': True,
                      'virtual_interface_id': vif['id']}
            self.db.fixed_ip_update(context, address, values)

        name = instance_ref['display_name']

        if self._validate_instance_zone_for_dns_domain(context, instance_ref):
            uuid = instance_ref['uuid']
            self.instance_dns_manager.create_entry(name, address,
                                                   "A",
                                                   self.instance_dns_domain)
            self.instance_dns_manager.create_entry(uuid, address,
                                                   "A",
                                                   self.instance_dns_domain)
        self._setup_network_on_host(context, network)
        return address

    def deallocate_fixed_ip(self, context, address, host=None, teardown=True):
        """Returns a fixed ip to the pool."""
        fixed_ip_ref = self.db.fixed_ip_get_by_address(context, address)
        vif_id = fixed_ip_ref['virtual_interface_id']
        instance = self.db.instance_get_by_uuid(
                context.elevated(read_deleted='yes'),
                fixed_ip_ref['instance_uuid'])

        self._do_trigger_security_group_members_refresh_for_instance(
            instance['uuid'])
        self._do_trigger_security_group_handler(
            'instance_remove_security_group', instance['uuid'])

        if self._validate_instance_zone_for_dns_domain(context, instance):
            for n in self.instance_dns_manager.get_entries_by_address(address,
                                                     self.instance_dns_domain):
                self.instance_dns_manager.delete_entry(n,
                                                      self.instance_dns_domain)

        self.db.fixed_ip_update(context, address,
                                {'allocated': False,
                                 'virtual_interface_id': None})

        if teardown:
            network = self._get_network_by_id(context,
                                              fixed_ip_ref['network_id'])
            self._teardown_network_on_host(context, network)

            if CONF.force_dhcp_release:
                dev = self.driver.get_dev(network)
                # NOTE(vish): The below errors should never happen, but there
                #             may be a race condition that is causing them per
                #             https://code.launchpad.net/bugs/968457, so we log
                #             an error to help track down the possible race.
                msg = _("Unable to release %s because vif doesn't exist.")
                if not vif_id:
                    LOG.error(msg % address)
                    return

                vif = self.db.virtual_interface_get(context, vif_id)

                if not vif:
                    LOG.error(msg % address)
                    return

                # NOTE(vish): This forces a packet so that the release_fixed_ip
                #             callback will get called by nova-dhcpbridge.
                self.driver.release_dhcp(dev, address, vif['address'])

    def lease_fixed_ip(self, context, address):
        """Called by dhcp-bridge when ip is leased."""
        LOG.debug(_('Leased IP |%(address)s|'), locals(), context=context)
        fixed_ip = self.db.fixed_ip_get_by_address(context, address)

        if fixed_ip['instance_uuid'] is None:
            msg = _('IP %s leased that is not associated') % address
            raise exception.NovaException(msg)
        now = timeutils.utcnow()
        self.db.fixed_ip_update(context,
                                fixed_ip['address'],
                                {'leased': True,
                                 'updated_at': now})
        if not fixed_ip['allocated']:
            LOG.warn(_('IP |%s| leased that isn\'t allocated'), address,
                     context=context)

    def release_fixed_ip(self, context, address):
        """Called by dhcp-bridge when ip is released."""
        LOG.debug(_('Released IP |%(address)s|'), locals(), context=context)
        fixed_ip = self.db.fixed_ip_get_by_address(context, address)

        if fixed_ip['instance_uuid'] is None:
            msg = _('IP %s released that is not associated') % address
            raise exception.NovaException(msg)
        if not fixed_ip['leased']:
            LOG.warn(_('IP %s released that was not leased'), address,
                     context=context)
        self.db.fixed_ip_update(context,
                                fixed_ip['address'],
                                {'leased': False})
        if not fixed_ip['allocated']:
            self.db.fixed_ip_disassociate(context, address)

    @staticmethod
    def _convert_int_args(kwargs):
        int_args = ("network_size", "num_networks",
                    "vlan_start", "vpn_start")
        for key in int_args:
            try:
                value = kwargs.get(key)
                if value is None:
                    continue
                kwargs[key] = int(value)
            except ValueError:
                raise ValueError(_("%s must be an integer") % key)

    def create_networks(self, context,
                        label, cidr=None, multi_host=None, num_networks=None,
                        network_size=None, cidr_v6=None,
                        gateway=None, gateway_v6=None, bridge=None,
                        bridge_interface=None, dns1=None, dns2=None,
                        fixed_cidr=None, **kwargs):
        arg_names = ("label", "cidr", "multi_host", "num_networks",
                     "network_size", "cidr_v6",
                     "gateway", "gateway_v6", "bridge",
                     "bridge_interface", "dns1", "dns2",
                     "fixed_cidr")
        for name in arg_names:
            kwargs[name] = locals()[name]
        self._convert_int_args(kwargs)

        # check for certain required inputs
        label = kwargs["label"]
        if not label:
            raise exception.NetworkNotCreated(req="label")

        # Size of "label" column in nova.networks is 255, hence the restriction
        if len(label) > 255:
            raise ValueError(_("Maximum allowed length for 'label' is 255."))

        if not (kwargs["cidr"] or kwargs["cidr_v6"]):
            raise exception.NetworkNotCreated(req="cidr or cidr_v6")

        kwargs["bridge"] = kwargs["bridge"] or CONF.flat_network_bridge
        kwargs["bridge_interface"] = (kwargs["bridge_interface"] or
                                      CONF.flat_interface)

        for fld in self.required_create_args:
            if not kwargs[fld]:
                raise exception.NetworkNotCreated(req=fld)

        kwargs["num_networks"] = kwargs["num_networks"] or CONF.num_networks
        if not kwargs["network_size"]:
            if kwargs["cidr"]:
                fixnet = netaddr.IPNetwork(kwargs["cidr"])
                each_subnet_size = fixnet.size / kwargs["num_networks"]
                if each_subnet_size > CONF.network_size:
                    subnet = 32 - int(math.log(CONF.network_size_size, 2))
                    oversize_msg = _(
                        'Subnet(s) too large, defaulting to /%s.'
                        '  To override, specify network_size flag.') % subnet
                    LOG.warn(oversize_msg)
                    kwargs["network_size"] = CONF.network_size
                else:
                    kwargs["network_size"] = fixnet.size
            else:
                kwargs["network_size"] = CONF.network_size

        kwargs["multi_host"] = (CONF.multi_host
                                if kwargs["multi_host"] is None
                                else
                                utils.bool_from_str(kwargs["multi_host"]))
        kwargs["vlan_start"] = kwargs.get("vlan_start") or CONF.vlan_start
        kwargs["vpn_start"] = kwargs.get("vpn_start") or CONF.vpn_start
        kwargs["dns1"] = kwargs["dns1"] or CONF.flat_network_dns

        if kwargs["fixed_cidr"]:
            kwargs["fixed_cidr"] = netaddr.IPNetwork(kwargs["fixed_cidr"])

        return self._do_create_networks(context, **kwargs)

    def _do_create_networks(self, context,
                            label, cidr, multi_host, num_networks,
                            network_size, cidr_v6, gateway, gateway_v6, bridge,
                            bridge_interface, dns1=None, dns2=None,
                            fixed_cidr=None, **kwargs):
        """Create networks based on parameters."""
        # NOTE(jkoelker): these are dummy values to make sure iter works
        # TODO(tr3buchet): disallow carving up networks
        fixed_net_v4 = netaddr.IPNetwork('0/32')
        fixed_net_v6 = netaddr.IPNetwork('::0/128')
        subnets_v4 = []
        subnets_v6 = []

        if kwargs.get('ipam'):
            if cidr_v6:
                subnets_v6 = [netaddr.IPNetwork(cidr_v6)]
            if cidr:
                subnets_v4 = [netaddr.IPNetwork(cidr)]
        else:
            subnet_bits = int(math.ceil(math.log(network_size, 2)))
            if cidr_v6:
                fixed_net_v6 = netaddr.IPNetwork(cidr_v6)
                prefixlen_v6 = 128 - subnet_bits
                # smallest subnet in IPv6 ethernet network is /64
                if prefixlen_v6 > 64:
                    prefixlen_v6 = 64
                subnets_v6 = fixed_net_v6.subnet(prefixlen_v6,
                                                 count=num_networks)
            if cidr:
                fixed_net_v4 = netaddr.IPNetwork(cidr)
                prefixlen_v4 = 32 - subnet_bits
                subnets_v4 = list(fixed_net_v4.subnet(prefixlen_v4,
                                                      count=num_networks))

        if cidr:
            # NOTE(jkoelker): This replaces the _validate_cidrs call and
            #                 prevents looping multiple times
            try:
                nets = self.db.network_get_all(context)
            except exception.NoNetworksFound:
                nets = []
            used_subnets = [netaddr.IPNetwork(net['cidr']) for net in nets]

            def find_next(subnet):
                next_subnet = subnet.next()
                while next_subnet in subnets_v4:
                    next_subnet = next_subnet.next()
                if next_subnet in fixed_net_v4:
                    return next_subnet

            for subnet in list(subnets_v4):
                if subnet in used_subnets:
                    next_subnet = find_next(subnet)
                    if next_subnet:
                        subnets_v4.remove(subnet)
                        subnets_v4.append(next_subnet)
                        subnet = next_subnet
                    else:
                        raise ValueError(_('cidr already in use'))
                for used_subnet in used_subnets:
                    if subnet in used_subnet:
                        msg = _('requested cidr (%(cidr)s) conflicts with '
                                'existing supernet (%(super)s)')
                        raise ValueError(msg % {'cidr': subnet,
                                                'super': used_subnet})
                    if used_subnet in subnet:
                        next_subnet = find_next(subnet)
                        if next_subnet:
                            subnets_v4.remove(subnet)
                            subnets_v4.append(next_subnet)
                            subnet = next_subnet
                        else:
                            msg = _('requested cidr (%(cidr)s) conflicts '
                                    'with existing smaller cidr '
                                    '(%(smaller)s)')
                            raise ValueError(msg % {'cidr': subnet,
                                                    'smaller': used_subnet})

        networks = []
        subnets = itertools.izip_longest(subnets_v4, subnets_v6)
        for index, (subnet_v4, subnet_v6) in enumerate(subnets):
            net = {}
            net['bridge'] = bridge
            net['bridge_interface'] = bridge_interface
            net['multi_host'] = multi_host

            net['dns1'] = dns1
            net['dns2'] = dns2

            net['project_id'] = kwargs.get('project_id')

            if num_networks > 1:
                net['label'] = '%s_%d' % (label, index)
            else:
                net['label'] = label

            if cidr and subnet_v4:
                net['cidr'] = str(subnet_v4)
                net['netmask'] = str(subnet_v4.netmask)
                net['gateway'] = gateway or str(subnet_v4[1])
                net['broadcast'] = str(subnet_v4.broadcast)
                net['dhcp_start'] = str(subnet_v4[2])

            if cidr_v6 and subnet_v6:
                net['cidr_v6'] = str(subnet_v6)
                if gateway_v6:
                    # use a pre-defined gateway if one is provided
                    net['gateway_v6'] = str(gateway_v6)
                else:
                    net['gateway_v6'] = str(subnet_v6[1])

                net['netmask_v6'] = str(subnet_v6._prefixlen)

            if kwargs.get('vpn', False):
                # this bit here is for vlan-manager
                vlan = kwargs['vlan_start'] + index
                net['vpn_private_address'] = str(subnet_v4[2])
                net['dhcp_start'] = str(subnet_v4[3])
                net['vlan'] = vlan
                net['bridge'] = 'br%s' % vlan

                # NOTE(vish): This makes ports unique across the cloud, a more
                #             robust solution would be to make them uniq per ip
                net['vpn_public_port'] = kwargs['vpn_start'] + index

            # None if network with cidr or cidr_v6 already exists
            network = self.db.network_create_safe(context, net)

            if not network:
                raise ValueError(_('Network already exists!'))
            else:
                networks.append(network)

            if network and cidr and subnet_v4:
                self._create_fixed_ips(context, network['id'], fixed_cidr)
        return networks

    @wrap_check_policy
    def delete_network(self, context, fixed_range, uuid,
            require_disassociated=True):

        # Prefer uuid but we'll also take cidr for backwards compatibility
        elevated = context.elevated()
        if uuid:
            network = self.db.network_get_by_uuid(elevated, uuid)
        elif fixed_range:
            network = self.db.network_get_by_cidr(elevated, fixed_range)

        if require_disassociated and network.project_id is not None:
            raise ValueError(_('Network must be disassociated from project %s'
                               ' before delete') % network.project_id)
        self.db.network_delete_safe(context, network.id)

    @property
    def _bottom_reserved_ips(self):  # pylint: disable=R0201
        """Number of reserved ips at the bottom of the range."""
        return 2  # network, gateway

    @property
    def _top_reserved_ips(self):  # pylint: disable=R0201
        """Number of reserved ips at the top of the range."""
        return 1  # broadcast

    def _create_fixed_ips(self, context, network_id, fixed_cidr=None):
        """Create all fixed ips for network."""
        network = self._get_network_by_id(context, network_id)
        # NOTE(vish): Should these be properties of the network as opposed
        #             to properties of the manager class?
        bottom_reserved = self._bottom_reserved_ips
        top_reserved = self._top_reserved_ips
        if not fixed_cidr:
            fixed_cidr = netaddr.IPNetwork(network['cidr'])
        num_ips = len(fixed_cidr)
        ips = []
        for index in range(num_ips):
            address = str(fixed_cidr[index])
            if index < bottom_reserved or num_ips - index <= top_reserved:
                reserved = True
            else:
                reserved = False

            ips.append({'network_id': network_id,
                        'address': address,
                        'reserved': reserved})
        self.db.fixed_ip_bulk_create(context, ips)

    def _allocate_fixed_ips(self, context, instance_id, host, networks,
                            **kwargs):
        """Calls allocate_fixed_ip once for each network."""
        raise NotImplementedError()

    def setup_networks_on_host(self, context, instance_id, host,
                               teardown=False):
        """calls setup/teardown on network hosts associated with an instance"""
        green_pool = greenpool.GreenPool()

        if teardown:
            call_func = self._teardown_network_on_host
        else:
            call_func = self._setup_network_on_host

        instance = self.db.instance_get(context, instance_id)
        vifs = self.db.virtual_interface_get_by_instance(context,
                                                         instance['uuid'])
        for vif in vifs:
            network = self.db.network_get(context, vif['network_id'])
            fixed_ips = self.db.fixed_ips_by_virtual_interface(context,
                                                               vif['id'])
            if not network['multi_host']:
                #NOTE (tr3buchet): if using multi_host, host is instance[host]
                host = network['host']
            if self.host == host or host is None:
                # at this point i am the correct host, or host doesn't
                # matter -> FlatManager
                call_func(context, network)
            else:
                # i'm not the right host, run call on correct host
                green_pool.spawn_n(
                        self.network_rpcapi.rpc_setup_network_on_host, context,
                        network['id'], teardown, host)

        # wait for all of the setups (if any) to finish
        green_pool.waitall()

    def rpc_setup_network_on_host(self, context, network_id, teardown):
        if teardown:
            call_func = self._teardown_network_on_host
        else:
            call_func = self._setup_network_on_host

        # subcall from original setup_networks_on_host
        network = self.db.network_get(context, network_id)
        call_func(context, network)

    def _setup_network_on_host(self, context, network):
        """Sets up network on this host."""
        raise NotImplementedError()

    def _teardown_network_on_host(self, context, network):
        """Sets up network on this host."""
        raise NotImplementedError()

    @wrap_check_policy
    def validate_networks(self, context, networks):
        """check if the networks exists and host
        is set to each network.
        """
        if networks is None or len(networks) == 0:
            return

        network_uuids = [uuid for (uuid, fixed_ip) in networks]

        self._get_networks_by_uuids(context, network_uuids)

        for network_uuid, address in networks:
            # check if the fixed IP address is valid and
            # it actually belongs to the network
            if address is not None:
                if not utils.is_valid_ipv4(address):
                    raise exception.FixedIpInvalid(address=address)

                fixed_ip_ref = self.db.fixed_ip_get_by_address(context,
                                                               address)
                network = self._get_network_by_id(context,
                                                  fixed_ip_ref['network_id'])
                if network['uuid'] != network_uuid:
                    raise exception.FixedIpNotFoundForNetwork(
                        address=address, network_uuid=network_uuid)
                if fixed_ip_ref['instance_uuid'] is not None:
                    raise exception.FixedIpAlreadyInUse(
                        address=address,
                        instance_uuid=fixed_ip_ref['instance_uuid'])

    def _get_network_by_id(self, context, network_id):
        return self.db.network_get(context, network_id,
                                   project_only="allow_none")

    def _get_networks_by_uuids(self, context, network_uuids):
        return self.db.network_get_all_by_uuids(context, network_uuids,
                                                project_only="allow_none")

    @wrap_check_policy
    def get_vifs_by_instance(self, context, instance_id):
        """Returns the vifs associated with an instance"""
        instance = self.db.instance_get(context, instance_id)
        vifs = self.db.virtual_interface_get_by_instance(context,
                                                         instance['uuid'])
        return [dict(vif.iteritems()) for vif in vifs]

    def get_instance_id_by_floating_address(self, context, address):
        """Returns the instance id a floating ip's fixed ip is allocated to"""
        floating_ip = self.db.floating_ip_get_by_address(context, address)
        if floating_ip['fixed_ip_id'] is None:
            return None

        fixed_ip = self.db.fixed_ip_get(context, floating_ip['fixed_ip_id'])

        # NOTE(tr3buchet): this can be None
        # NOTE(mikal): we need to return the instance id here because its used
        # by ec2 (and possibly others)
        uuid = fixed_ip['instance_uuid']
        if not uuid:
            return uuid

        instance = self.db.instance_get_by_uuid(context, uuid)
        return instance['id']

    @wrap_check_policy
    def get_network(self, context, network_uuid):
        network = self.db.network_get_by_uuid(context.elevated(), network_uuid)
        return dict(network.iteritems())

    @wrap_check_policy
    def get_all_networks(self, context):
        try:
            networks = self.db.network_get_all(context)
        except exception.NoNetworksFound:
            return []
        return [dict(network.iteritems()) for network in networks]

    @wrap_check_policy
    def disassociate_network(self, context, network_uuid):
        network = self.get_network(context, network_uuid)
        self.db.network_disassociate(context, network['id'])

    @wrap_check_policy
    def get_fixed_ip(self, context, id):
        """Return a fixed ip"""
        fixed = self.db.fixed_ip_get(context, id)
        return dict(fixed.iteritems())

    @wrap_check_policy
    def get_fixed_ip_by_address(self, context, address):
        fixed = self.db.fixed_ip_get_by_address(context, address)
        return dict(fixed.iteritems())

    def get_vif_by_mac_address(self, context, mac_address):
        """Returns the vifs record for the mac_address"""
        return self.db.virtual_interface_get_by_address(context,
                                                        mac_address)

    def update_dns(self, context, network_ids):
        """Called when fixed IP is allocated or deallocated"""
        if CONF.fake_network:
            return

        for network_id in network_ids:
            network = self.db.network_get(context, network_id)
            if not network['multi_host']:
                continue
            host_networks = self.db.network_get_all_by_host(context, self.host)
            for host_network in host_networks:
                if host_network['id'] == network_id:
                    dev = self.driver.get_dev(network)
                    self.driver.update_dns(context, dev, network)


class FlatManager(NetworkManager):
    """Basic network where no vlans are used.

    FlatManager does not do any bridge or vlan creation.  The user is
    responsible for setting up whatever bridges are specified when creating
    networks through nova-manage. This bridge needs to be created on all
    compute hosts.

    The idea is to create a single network for the host with a command like:
    nova-manage network create 192.168.0.0/24 1 256. Creating multiple
    networks for for one manager is currently not supported, but could be
    added by modifying allocate_fixed_ip and get_network to get the a network
    with new logic instead of network_get_by_bridge. Arbitrary lists of
    addresses in a single network can be accomplished with manual db editing.

    If flat_injected is True, the compute host will attempt to inject network
    config into the guest.  It attempts to modify /etc/network/interfaces and
    currently only works on debian based systems. To support a wider range of
    OSes, some other method may need to be devised to let the guest know which
    ip it should be using so that it can configure itself. Perhaps an attached
    disk or serial device with configuration info.

    Metadata forwarding must be handled by the gateway, and since nova does
    not do any setup in this mode, it must be done manually.  Requests to
    169.254.169.254 port 80 will need to be forwarded to the api server.

    """

    timeout_fixed_ips = False

    required_create_args = ['bridge']

    def _allocate_fixed_ips(self, context, instance_id, host, networks,
                            **kwargs):
        """Calls allocate_fixed_ip once for each network."""
        requested_networks = kwargs.get('requested_networks')
        for network in networks:
            address = None
            if requested_networks is not None:
                for address in (fixed_ip for (uuid, fixed_ip) in
                                requested_networks if network['uuid'] == uuid):
                    break

            self.allocate_fixed_ip(context, instance_id,
                                   network, address=address)

    def deallocate_fixed_ip(self, context, address, host=None, teardown=True):
        """Returns a fixed ip to the pool."""
        super(FlatManager, self).deallocate_fixed_ip(context, address, host,
                                                     teardown)
        self.db.fixed_ip_disassociate(context, address)

    def _setup_network_on_host(self, context, network):
        """Setup Network on this host."""
        # NOTE(tr3buchet): this does not need to happen on every ip
        # allocation, this functionality makes more sense in create_network
        # but we'd have to move the flat_injected flag to compute
        net = {}
        net['injected'] = CONF.flat_injected
        self.db.network_update(context, network['id'], net)

    def _teardown_network_on_host(self, context, network):
        """Tear down network on this host."""
        pass

    # NOTE(justinsb): The floating ip functions are stub-implemented.
    # We were throwing an exception, but this was messing up horizon.
    # Timing makes it difficult to implement floating ips here, in Essex.

    @wrap_check_policy
    def get_floating_ip(self, context, id):
        """Returns a floating IP as a dict"""
        return None

    @wrap_check_policy
    def get_floating_pools(self, context):
        """Returns list of floating pools"""
        return {}

    @wrap_check_policy
    def get_floating_ip_by_address(self, context, address):
        """Returns a floating IP as a dict"""
        return None

    @wrap_check_policy
    def get_floating_ips_by_project(self, context):
        """Returns the floating IPs allocated to a project"""
        return []

    @wrap_check_policy
    def get_floating_ips_by_fixed_address(self, context, fixed_address):
        """Returns the floating IPs associated with a fixed_address"""
        return []

    def migrate_instance_start(self, context, instance_uuid,
                               floating_addresses,
                               rxtx_factor=None, project_id=None,
                               source=None, dest=None):
        pass

    def migrate_instance_finish(self, context, instance_uuid,
                                floating_addresses, host=None,
                                rxtx_factor=None, project_id=None,
                                source=None, dest=None):
        pass

    def update_dns(self, context, network_ids):
        """Called when fixed IP is allocated or deallocated"""
        pass


class FlatDHCPManager(RPCAllocateFixedIP, FloatingIP, NetworkManager):
    """Flat networking with dhcp.

    FlatDHCPManager will start up one dhcp server to give out addresses.
    It never injects network settings into the guest. It also manages bridges.
    Otherwise it behaves like FlatManager.

    """

    SHOULD_CREATE_BRIDGE = True
    DHCP = True
    required_create_args = ['bridge']

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """
        self.l3driver.initialize()
        super(FlatDHCPManager, self).init_host()
        self.init_host_floating_ips()

    def _setup_network_on_host(self, context, network):
        """Sets up network on this host."""
        network['dhcp_server'] = self._get_dhcp_ip(context, network)

        self.l3driver.initialize_gateway(network)

        if not CONF.fake_network:
            dev = self.driver.get_dev(network)
            self.driver.update_dhcp(context, dev, network)
            if(CONF.use_ipv6):
                self.driver.update_ra(context, dev, network)
                gateway = utils.get_my_linklocal(dev)
                self.db.network_update(context, network['id'],
                                       {'gateway_v6': gateway})

    def _teardown_network_on_host(self, context, network):
        if not CONF.fake_network:
            network['dhcp_server'] = self._get_dhcp_ip(context, network)
            dev = self.driver.get_dev(network)
            self.driver.update_dhcp(context, dev, network)

    def _get_network_dict(self, network):
        """Returns the dict representing necessary and meta network fields"""

        # get generic network fields
        network_dict = super(FlatDHCPManager, self)._get_network_dict(network)

        # get flat dhcp specific fields
        if self.SHOULD_CREATE_BRIDGE:
            network_dict['should_create_bridge'] = self.SHOULD_CREATE_BRIDGE
        if network.get('bridge_interface'):
            network_dict['bridge_interface'] = network['bridge_interface']
        if network.get('multi_host'):
            network_dict['multi_host'] = network['multi_host']

        return network_dict


class VlanManager(RPCAllocateFixedIP, FloatingIP, NetworkManager):
    """Vlan network with dhcp.

    VlanManager is the most complicated.  It will create a host-managed
    vlan for each project.  Each project gets its own subnet.  The networks
    and associated subnets are created with nova-manage using a command like:
    nova-manage network create 10.0.0.0/8 3 16.  This will create 3 networks
    of 16 addresses from the beginning of the 10.0.0.0 range.

    A dhcp server is run for each subnet, so each project will have its own.
    For this mode to be useful, each project will need a vpn to access the
    instances in its subnet.

    """

    SHOULD_CREATE_BRIDGE = True
    SHOULD_CREATE_VLAN = True
    DHCP = True
    required_create_args = ['bridge_interface']

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """

        self.l3driver.initialize()
        NetworkManager.init_host(self)
        self.init_host_floating_ips()

    def allocate_fixed_ip(self, context, instance_id, network, **kwargs):
        """Gets a fixed ip from the pool."""
        instance = self.db.instance_get(context, instance_id)

        if kwargs.get('vpn', None):
            address = network['vpn_private_address']
            self.db.fixed_ip_associate(context,
                                       address,
                                       instance['uuid'],
                                       network['id'],
                                       reserved=True)
        else:
            address = kwargs.get('address', None)
            if address:
                address = self.db.fixed_ip_associate(context, address,
                                                     instance['uuid'],
                                                     network['id'])
            else:
                address = self.db.fixed_ip_associate_pool(context,
                                                          network['id'],
                                                          instance['uuid'])
            self._do_trigger_security_group_members_refresh_for_instance(
                                                                   instance_id)

        vif = self.db.virtual_interface_get_by_instance_and_network(
            context, instance['uuid'], network['id'])
        values = {'allocated': True,
                  'virtual_interface_id': vif['id']}
        self.db.fixed_ip_update(context, address, values)

        if self._validate_instance_zone_for_dns_domain(context, instance):
            name = instance['display_name']
            uuid = instance['uuid']
            self.instance_dns_manager.create_entry(name, address,
                                                   "A",
                                                   self.instance_dns_domain)
            self.instance_dns_manager.create_entry(uuid, address,
                                                   "A",
                                                   self.instance_dns_domain)

        self._setup_network_on_host(context, network)
        return address

    @wrap_check_policy
    def add_network_to_project(self, context, project_id, network_uuid=None):
        """Force adds another network to a project."""
        if network_uuid is not None:
            network_id = self.get_network(context, network_uuid)['id']
        else:
            network_id = None
        self.db.network_associate(context, project_id, network_id, force=True)

    def _get_network_by_id(self, context, network_id):
        # NOTE(vish): Don't allow access to networks with project_id=None as
        #             these are networksa that haven't been allocated to a
        #             project yet.
        return self.db.network_get(context, network_id, project_only=True)

    def _get_networks_by_uuids(self, context, network_uuids):
        # NOTE(vish): Don't allow access to networks with project_id=None as
        #             these are networksa that haven't been allocated to a
        #             project yet.
        return self.db.network_get_all_by_uuids(context, network_uuids,
                                                project_only=True)

    def _get_networks_for_instance(self, context, instance_id, project_id,
                                   requested_networks=None):
        """Determine which networks an instance should connect to."""
        # get networks associated with project
        if requested_networks is not None and len(requested_networks) != 0:
            network_uuids = [uuid for (uuid, fixed_ip) in requested_networks]
            networks = self._get_networks_by_uuids(context, network_uuids)
        else:
            networks = self.db.project_get_networks(context, project_id)
        return networks

    def create_networks(self, context, **kwargs):
        """Create networks based on parameters."""
        self._convert_int_args(kwargs)

        # Check that num_networks + vlan_start is not > 4094, fixes lp708025
        if kwargs['num_networks'] + kwargs['vlan_start'] > 4094:
            raise ValueError(_('The sum between the number of networks and'
                               ' the vlan start cannot be greater'
                               ' than 4094'))

        # check that num networks and network size fits in fixed_net
        fixed_net = netaddr.IPNetwork(kwargs['cidr'])
        if len(fixed_net) < kwargs['num_networks'] * kwargs['network_size']:
            raise ValueError(_('The network range is not big enough to fit '
                  '%(num_networks)s. Network size is %(network_size)s') %
                  kwargs)

        kwargs['bridge_interface'] = (kwargs.get('bridge_interface') or
                                      CONF.vlan_interface)
        return NetworkManager.create_networks(
            self, context, vpn=True, **kwargs)

    def _setup_network_on_host(self, context, network):
        """Sets up network on this host."""
        if not network['vpn_public_address']:
            net = {}
            address = CONF.vpn_ip
            net['vpn_public_address'] = address
            network = self.db.network_update(context, network['id'], net)
        else:
            address = network['vpn_public_address']
        network['dhcp_server'] = self._get_dhcp_ip(context, network)

        self.l3driver.initialize_gateway(network)

        # NOTE(vish): only ensure this forward if the address hasn't been set
        #             manually.
        if address == CONF.vpn_ip and hasattr(self.driver,
                                               "ensure_vpn_forward"):
            self.l3driver.add_vpn(CONF.vpn_ip,
                    network['vpn_public_port'],
                    network['vpn_private_address'])
        if not CONF.fake_network:
            dev = self.driver.get_dev(network)
            self.driver.update_dhcp(context, dev, network)
            if(CONF.use_ipv6):
                self.driver.update_ra(context, dev, network)
                gateway = utils.get_my_linklocal(dev)
                self.db.network_update(context, network['id'],
                                       {'gateway_v6': gateway})

    def _teardown_network_on_host(self, context, network):
        if not CONF.fake_network:
            network['dhcp_server'] = self._get_dhcp_ip(context, network)
            dev = self.driver.get_dev(network)
            self.driver.update_dhcp(context, dev, network)

    def _get_network_dict(self, network):
        """Returns the dict representing necessary and meta network fields"""

        # get generic network fields
        network_dict = super(VlanManager, self)._get_network_dict(network)

        # get vlan specific network fields
        if self.SHOULD_CREATE_BRIDGE:
            network_dict['should_create_bridge'] = self.SHOULD_CREATE_BRIDGE
        if self.SHOULD_CREATE_VLAN:
            network_dict['should_create_vlan'] = self.SHOULD_CREATE_VLAN
        for k in ['vlan', 'bridge_interface', 'multi_host']:
            if network.get(k):
                network_dict[k] = network[k]

        return network_dict

    @property
    def _bottom_reserved_ips(self):
        """Number of reserved ips at the bottom of the range."""
        return super(VlanManager, self)._bottom_reserved_ips + 1  # vpn server

    @property
    def _top_reserved_ips(self):
        """Number of reserved ips at the top of the range."""
        parent_reserved = super(VlanManager, self)._top_reserved_ips
        return parent_reserved + CONF.cnt_vpn_clients
