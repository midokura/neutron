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

import abc

import six

from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class LinuxBGPDriver(object):
    """Base class for the BGP drivers.

    Every class provides the features of the BGP speakers should extend this
    base class.
    """
    def __init__(self, as_number, router_id,
                 best_path_change_handler=None):
        self.as_number = as_number
        self.router_id = router_id
        self.best_path_change_handler = best_path_change_handler

    @abc.abstractmethod
    def add_peer(self, peer_id, peer_as, password=None):
        """Add a new routing peer.

        :param peer_id: the peer ID
        :param peer_as: AS number of the peer
        :param password: the password used for securing sessions; defaults to
            None
        """

    @abc.abstractmethod
    def del_peer(self, peer_id):
        """Delete a routing peer associated with the given peer ID

        :param peer_id: the peer ID
        """
