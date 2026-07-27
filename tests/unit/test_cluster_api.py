import unittest
from unittest.mock import MagicMock

from kubernetes import client

from mysqloperator.controller.innodbcluster.cluster_api import InnoDBCluster, MySQLPod


class PodOwnershipTest(unittest.TestCase):
    def test_owner_reference_tolerates_missing_owner_references(self):
        pod = MySQLPod(client.V1Pod(metadata=client.V1ObjectMeta(name="orphan-pod")))

        self.assertIsNone(pod.owner_reference("apps/v1", "StatefulSet"))

    def test_cluster_does_not_own_pod_without_statefulset_owner(self):
        cluster = MagicMock(name="test-cluster")
        cluster.name = "test-cluster"
        pod = MySQLPod(client.V1Pod(metadata=client.V1ObjectMeta(name="orphan-pod")))

        self.assertFalse(InnoDBCluster.owns_pod(cluster, pod))

    def test_cluster_owns_pod_with_matching_statefulset_owner(self):
        owner = client.V1OwnerReference(
            api_version="apps/v1",
            kind="StatefulSet",
            name="test-cluster",
            uid="test-uid",
        )
        pod = MySQLPod(client.V1Pod(metadata=client.V1ObjectMeta(
            name="test-cluster-0", owner_references=[owner])))
        cluster = MagicMock(name="test-cluster")
        cluster.name = "test-cluster"

        self.assertTrue(InnoDBCluster.owns_pod(cluster, pod))
