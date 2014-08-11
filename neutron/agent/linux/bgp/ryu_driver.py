# Copyright 2014 Midokura SARL.  All rights reserved.
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

from ryu.services.protocols.bgp import bgpspeaker

from neutron.agent.linux.bgp import base
from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)


class RyuBGPDriver(base.LinuxBGPDriver):
    """BGP driver with Ryu's BGPSpeaker."""

    def __init__(self, as_number, router_id,
                 best_path_change_handler=None):
        self.bgp_speaker = bgpspeaker.BGPSpeaker(
            as_number, router_id,
            best_path_change_handler=best_path_change_handler)
        super(RyuBGPDriver, self).__init__(as_number, router_id,
                                           best_path_change_handler)

    def add_peer(self, peer_id, peer_as, password=None):
        self.bgp_speaker.neighbor_add(peer_id, peer_as, password=password)

    def del_peer(self, peer_id):
        self.bgp_speaker.neighbor_del(peer_id)
