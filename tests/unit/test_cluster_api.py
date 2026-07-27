import json
import unittest
from unittest.mock import MagicMock, patch

from kubernetes import client

from mysqloperator.controller import kubeutils
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


class PodMembershipUpdateTest(unittest.TestCase):
    def make_pod(self, annotations=None, labels=None, conditions=None):
        return MySQLPod(client.V1Pod(
            metadata=client.V1ObjectMeta(
                name="test-cluster-0",
                namespace="test-ns",
                annotations=annotations,
                labels=labels,
            ),
            status=client.V1PodStatus(conditions=conditions),
        ))

    def test_readiness_gate_does_not_patch_when_value_is_unchanged(self):
        condition = client.V1PodCondition(
            type="mysql.oracle.com/ready",
            status="True",
        )
        pod = self.make_pod(conditions=[condition])

        with patch.object(kubeutils.api_core,
                          "patch_namespaced_pod_status") as patch_status:
            pod.update_member_readiness_gate("ready", True)

        patch_status.assert_not_called()

    def test_readiness_gate_patches_when_value_changes(self):
        condition = client.V1PodCondition(
            type="mysql.oracle.com/ready",
            status="False",
        )
        pod = self.make_pod(conditions=[condition])

        with patch.object(kubeutils.api_core,
                          "patch_namespaced_pod_status",
                          return_value=pod.pod) as patch_status, \
             patch("mysqloperator.controller.innodbcluster.cluster_api.utils.isotime",
                   return_value="2026-07-27T00:00:00Z"):
            pod.update_member_readiness_gate("ready", True)

        patch_status.assert_called_once()
        condition_patch = patch_status.call_args.kwargs["body"]["status"]["conditions"][0]
        self.assertEqual(condition_patch["lastTransitionTime"],
                         "2026-07-27T00:00:00Z")

    def test_membership_does_not_patch_when_semantics_are_unchanged(self):
        info = {
            "memberId": "member-1",
            "lastTransitionTime": "2026-07-26T00:00:00Z",
            "lastProbeTime": "2026-07-26T00:01:00Z",
            "groupViewId": "view-1",
            "status": "ONLINE",
            "version": "9.6.0",
            "role": "PRIMARY",
        }
        pod = self.make_pod(
            annotations={"mysql.oracle.com/membership-info":
                         json.dumps(info)},
            labels={"mysql.oracle.com/cluster-role": "PRIMARY"},
        )

        with patch.object(kubeutils.api_core,
                          "patch_namespaced_pod") as patch_pod:
            pod.update_membership_status(
                "member-1", "PRIMARY", "ONLINE", "view-1", "9.6.0")

        patch_pod.assert_not_called()

    def test_version_change_patches_without_changing_transition_time(self):
        info = {
            "memberId": "member-1",
            "lastTransitionTime": "2026-07-26T00:00:00Z",
            "lastProbeTime": "2026-07-26T00:01:00Z",
            "groupViewId": "view-1",
            "status": "ONLINE",
            "version": "9.5.0",
            "role": "PRIMARY",
        }
        pod = self.make_pod(
            annotations={"mysql.oracle.com/membership-info":
                         json.dumps(info)},
            labels={"mysql.oracle.com/cluster-role": "PRIMARY"},
        )

        with patch.object(kubeutils.api_core,
                          "patch_namespaced_pod",
                          return_value=pod.pod) as patch_pod, \
             patch("mysqloperator.controller.innodbcluster.cluster_api.utils.isotime",
                   return_value="2026-07-27T00:00:00Z"):
            pod.update_membership_status(
                "member-1", "PRIMARY", "ONLINE", "view-1", "9.6.0")

        membership = json.loads(
            patch_pod.call_args.args[2]["metadata"]["annotations"]
            ["mysql.oracle.com/membership-info"])
        self.assertEqual(membership["lastTransitionTime"],
                         "2026-07-26T00:00:00Z")
        self.assertEqual(membership["lastProbeTime"],
                         "2026-07-27T00:00:00Z")
