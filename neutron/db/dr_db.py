# Copyright 2011 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.common import utils
from neutron.db import db_base_plugin_v2 as base_db
from neutron.db import extraroute_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import dynamic_routing as dr
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils


LOG = logging.getLogger(__name__)


class RoutingPeer(model_base.BASEV2,
                  models_v2.HasId,
                  models_v2.HasTenant):
    """Represents a routing peer."""

    peer = sa.Column(sa.String(64))
    remote_as = sa.Column(sa.Integer)
    password = sa.Column(sa.String(255))
    extra_config = sa.Column(sa.String(4096))


class RoutingInstance(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """ Represents a routing instance."""

    nexthop = sa.Column(sa.String(64))
    advertise = sa.Column(sa.Boolean)
    discover = sa.Column(sa.Boolean)


class RoutingInstanceNetBinding(model_base.BASEV2, models_v2.HasId):
    """Binding between routing instance and networks"""
    __tablename__ = "routinginstancenetworkbindings"

    routinginstance_id = sa.Column(sa.String(36),
                                   sa.ForeignKey("routinginstances.id",
                                                 ondelete='CASCADE'))
    routinginstance = orm.relation(RoutingInstance)
    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey("networks.id",
                                         ondelete='CASCADE'))


class AdvertiseRoute(model_base.BASEV2):
    advertise_route = sa.Column(sa.String(length=64),
                                nullable=False,
                                primary_key=True)
    routinginstance_id = sa.Column(sa.String(36),
                                   sa.ForeignKey("routinginstances.id",
                                                 ondelete="CASCADE"),
                                   primary_key=True)
    routinginstance = orm.relationship(RoutingInstance,
                                       backref=orm.backref("advertise_routes",
                                                           lazy='joined',
                                                           cascade='delete'))


class DynamicRoutingDbMixin(dr.DynamicRoutingPluginBase,
                            base_db.NeutronDbPluginV2,
                            extraroute_db.ExtraRoute_db_mixin):

    def create_routingpeer(self, context, routingpeer):
        LOG.debug(_("create_routingpeer() called"))
        rp = routingpeer['routingpeer']
        try:
            rp = self._get_routingpeer_by_peer(
                 context, routingpeer['routingpeer']['peer'])
            raise dr.PeerExists(peer=routingpeer['routingpeer']['peer'])
        except dr.RoutingPeerNotFound:
            pass

        with context.session.begin(subtransactions=True):
            res_keys = ['peer',
                        'remote_as',
                        'password',
                        'tenant_id',
                        'extra_config']
            res = dict((k, rp[k]) for k in res_keys)
            configuration_dict = rp.get('extra_config', {})
            res['extra_config'] = jsonutils.dumps(configuration_dict)
            res['id'] = uuidutils.generate_uuid()
            routingpeer_db = RoutingPeer(**res)
            context.session.add(routingpeer_db)
            context.session.flush()

        return self._make_routingpeer_dict(routingpeer_db)

    def create_routinginstance(self, context, routinginstance):
        LOG.debug(_("create_routinginstance() called"))
        rp = routinginstance['routinginstance']
        with context.session.begin(subtransactions=True):
            res_keys = ['advertise', 'discover', 'nexthop', 'tenant_id']
            res = dict((k, rp[k]) for k in res_keys)
            res['id'] = uuidutils.generate_uuid()
            routinginstance_db = RoutingInstance(**res)
            context.session.add(routinginstance_db)

        return self._make_routinginstance_dict(routinginstance_db)

    def delete_routingpeer(self, context, routingpeer_id):
        LOG.debug(_("delete_routingpeer() called"))
        with context.session.begin(subtransactions=True):
            routingpeer = self._get_routingpeer(context, routingpeer_id)
            context.session.delete(routingpeer)

    def delete_routinginstance(self, context, routinginstance_id):
        LOG.debug(_("delete_routinginstance() called"))
        with context.session.begin(subtransactions=True):
            routinginstance = self._get_routinginstance(
                context, routinginstance_id)
            context.session.delete(routinginstance)

    def get_routingpeers(self, context, filters=None, fields=None):
        LOG.debug(_("get_routingpeers() called"))
        return self._get_collection(context, RoutingPeer,
                                    self._make_routingpeer_dict,
                                    filters=filters,
                                    fields=fields)

    def get_routingpeer(self, context, id, fields=None):
        LOG.debug(_("get_routingpeer() called"))
        routingpeer = self._get_routingpeer(context, id)
        return self._make_routingpeer_dict(routingpeer, fields)

    def get_routinginstance(self, context, id, fields=None):
        LOG.debug(_("get_routinginstance() called"))
        routinginstance = self._get_routinginstance(context, id)
        return self._make_routinginstance_dict(routinginstance, fields)

    def update_routingpeer(self, context, id, routingpeer):
        LOG.debug(_("update_routingpeer() called"))
        rp = routingpeer['routingpeer']
        with context.session.begin(subtransactions=True):
            routingpeer = self._get_routingpeer(context, id)
            routingpeer.update(rp)
            return self._make_routingpeer_dict(routingpeer)

    def update_routinginstance(self, context, id, routinginstance):
        LOG.debug(_("update_routinginstance() called"))
        data = routinginstance['routinginstance']
        with context.session.begin(subtransactions=True):
            routinginstance_db = self._get_routinginstance(context, id)
            if 'advertise_routes' in data:
                self._update_advertise_routes(
                    context, id, data['advertise_routes'])
                data.pop('advertise_routes')
            routes = self._get_advertise_routes_by_routinginstance(
                context, id)
            routinginstance_db.update(data)
            ri_dict = self._make_routinginstance_dict(routinginstance_db)
            ri_dict['advertise_routes'] = routes
            return ri_dict

    def _get_advertise_routes_by_routinginstance(self, context,
                                                 routinginstance_id):
        query = context.session.query(AdvertiseRoute)
        query = query.filter_by(routinginstance_id=routinginstance_id)
        return self._make_advertise_route_list(query)

    def _get_routes_dict_by_instance_id(self, context,
                                        routinginstance_id):
        query = context.session.query(AdvertiseRoute)
        query = query.filter_by(routinginstance_id=routinginstance_id)
        routes = []
        routes_dict = {}
        for advertise_route in query:
            routes.append({'advertise_route':
                           advertise_route['advertise_route']})
            routes_dict[advertise_route['advertise_route']] = advertise_route
        return routes, routes_dict

    def _update_advertise_routes(self, context, routinginstance_id,
                                 advertise_routes):
        old_routes, routes_dict = self._get_routes_dict_by_instance_id(
            context, routinginstance_id)
        new_routes = [{'advertise_route': route} for route in advertise_routes]
        added, removed = utils.diff_list_of_dict(old_routes, new_routes)
        LOG.debug(_('Added routes are %s'), added)
        for route in added:
            advertise_route = AdvertiseRoute(
                advertise_route=route['advertise_route'],
                routinginstance_id=routinginstance_id)
            context.session.add(advertise_route)

        LOG.debug(_('Removed routes are %s'), removed)
        for route in removed:
            context.session.delete(routes_dict[route['advertise_route']])

    def add_network_to_routinginstance(self, context, routinginstance_id,
                                       network_id):
        """Associate a network to a routing instance."""
        with context.session.begin(subtransactions=True):
            routinginstance = self._get_routinginstance(
                    context,
                    routinginstance_id)
            query = context.session.query(RoutingInstanceNetBinding)
            query = query.filter(
                    RoutingInstanceNetBinding.routinginstance_id ==
                    routinginstance_id,
                    RoutingInstanceNetBinding.network_id == network_id)

            try:
                binding = query.one()
                raise dr.NetworkAlreadyAssociated(
                    routinginstance_id=binding.routinginstance_id,
                    network_id=network_id)
            except exc.NoResultFound:
                pass

            binding = RoutingInstanceNetBinding()
            binding.routinginstance_id = routinginstance.id
            binding.network_id = network_id
            context.session.add(binding)
            LOG.debug(_('Network %(network_id)s is associated to '
                        'routing instance %(routinginstance_id)s'),
                      {'routinginstance_id': routinginstance.id,
                       'network_id': network_id})

    def remove_network_from_routinginstance(self, context, routinginstance_id,
                                            network_id):

        with context.session.begin(subtransactions=True):
            routinginstance_db = self._get_routinginstance(
                context, routinginstance_id)
            query = context.session.query(RoutingInstanceNetBinding)
            query = query.filter(
                    RoutingInstanceNetBinding.routinginstance_id ==
                    routinginstance_db.id,
                    RoutingInstanceNetBinding.network_id == network_id)
            try:
                binding = query.one()
            except exc.NoResultFound:
                raise dr.RoutingInstanceNetNotHosted(ri_id=routinginstance_id,
                                                     network_id=network_id)
            context.session.delete(binding)

    def get_routinginstances(self, context, filters=None, fields=None):
        LOG.debug(_("get_routinginstances() called"))
        coll = self._get_collection(context, RoutingInstance,
                                    self._make_routinginstance_dict,
                                    filters=filters,
                                    fields=fields)
        return coll

    def list_networks_on_routinginstance(self, context, routinginstance_id):
        LOG.debug(_("list_networks_on_routinginstance() called"))
        query = context.session.query(RoutingInstanceNetBinding.network_id)
        query = query.filter(RoutingInstanceNetBinding.routinginstance_id ==
                routinginstance_id)

        network_ids = [item[0] for item in query]
        if network_ids:
            return {'networks':
                    self.get_networks(context, filters={'id': network_ids})}
        else:
            return {'networks': []}

    def _get_routingpeer(self, context, id):
        try:
            return self._get_by_id(context, RoutingPeer, id)
        except exc.NoResultFound:
            raise dr.RoutingPeerNotFound(routingpeer_id=id)

    def _get_routingpeer_by_peer(self, context, peer):
        try:
            query = self._model_query(context, RoutingPeer)
            return query.filter(RoutingPeer.peer == peer).one()
        except exc.NoResultFound:
            raise dr.RoutingPeerNotFound(routingpeer_id=peer)

    def _get_routinginstance(self, context, id):
        try:
            return self._get_by_id(context, RoutingInstance, id)
        except exc.NoResultFound:
            raise dr.RoutingInstanceNotFound(routinginstance_id=id)

    def _make_routinginstance_dict(self, routinginstance, fields=None):
        attr = dr.RESOURCE_ATTRIBUTE_MAP.get('routinginstances')
        res = {k: routinginstance[k] for k in attr if k not in
               ['advertise_routes']}
        res['advertise_routes'] = self._make_advertise_route_list(
                                      routinginstance['advertise_routes'])
        return res

    def _make_routingpeer_dict(self, routingpeer, fields=None):
        attr = dr.RESOURCE_ATTRIBUTE_MAP.get('routingpeers')
        res = {k: routingpeer[k] for k in attr if k not in ['extra_config']}
        if routingpeer['extra_config']:
            res['extra_config'] = jsonutils.loads(routingpeer['extra_config'])
        return res

    def _make_advertise_route_list(self, advertise_routes):
        return [route['advertise_route'] for route in advertise_routes]
