import unittest
from unittest.mock import MagicMock, patch

from mysqloperator.controller import diagnose
from mysqloperator.controller.innodbcluster.cluster_controller import ClusterController


class PublishClusterStatusTest(unittest.TestCase):
    def setUp(self):
        self.cluster = MagicMock()
        self.cluster.parsed_spec.initDB = None
        self.controller = ClusterController(self.cluster)
        self.logger = MagicMock()
        self.diag = MagicMock()
        self.diag.status = diagnose.ClusterDiagStatus.ONLINE
        self.diag.online_members = ["pod-0", "pod-1", "pod-2"]
        self.diag.type = diagnose.ClusterInClusterSetType.PRIMARY

    def test_does_not_patch_when_semantic_status_is_unchanged(self):
        self.cluster.get_cluster_status.return_value = {
            "status": "ONLINE",
            "onlineInstances": 3,
            "type": "PRIMARY",
            "lastProbeTime": "2026-07-26T00:00:00Z",
        }

        with patch(
                "mysqloperator.controller.innodbcluster.cluster_controller"
                ".utils.isotime") as isotime:
            self.controller.publish_status(self.diag, self.logger)

        self.cluster.set_cluster_status.assert_not_called()
        isotime.assert_not_called()

    def test_patches_and_updates_probe_time_when_status_changes(self):
        self.cluster.get_cluster_status.return_value = {
            "status": "ONLINE_PARTIAL",
            "onlineInstances": 2,
            "type": "PRIMARY",
            "lastProbeTime": "2026-07-26T00:00:00Z",
        }

        with patch(
                "mysqloperator.controller.innodbcluster.cluster_controller"
                ".utils.isotime",
                return_value="2026-07-27T00:00:00Z"):
            self.controller.publish_status(self.diag, self.logger)

        self.cluster.set_cluster_status.assert_called_once_with({
            "status": "ONLINE",
            "onlineInstances": 3,
            "type": "PRIMARY",
            "lastProbeTime": "2026-07-27T00:00:00Z",
        })


class PodDeletionTest(unittest.TestCase):
    def make_pod(self):
        pod = MagicMock()
        pod.name = "test-cluster-1"
        pod_body = {
            "metadata": {
                "name": pod.name,
                "finalizers": ["mysql.oracle.com/membership"],
            }
        }
        return pod, pod_body

    def test_probe_failure_does_not_leave_membership_finalizer(self):
        cluster = MagicMock()
        cluster.deleting = False
        controller = ClusterController(cluster)
        controller.probe_status = MagicMock(
            side_effect=RuntimeError("diagnosis failed"))
        pod, pod_body = self.make_pod()
        logger = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "diagnosis failed"):
            controller.on_pod_deleted(pod, pod_body, logger)

        pod.remove_member_finalizer.assert_called_once_with(pod_body)

    def test_probe_failure_while_cluster_is_deleting_removes_finalizer(self):
        cluster = MagicMock()
        cluster.deleting = True
        controller = ClusterController(cluster)
        controller.probe_status = MagicMock(
            side_effect=RuntimeError("diagnosis failed"))
        pod, pod_body = self.make_pod()

        with self.assertRaisesRegex(RuntimeError, "diagnosis failed"):
            controller.on_pod_deleted(pod, pod_body, MagicMock())

        pod.remove_member_finalizer.assert_called_once_with(pod_body)

    def test_destroy_failure_does_not_leave_membership_finalizer(self):
        cluster = MagicMock()
        cluster.deleting = True
        controller = ClusterController(cluster)
        diag = MagicMock()
        diag.online_members = []
        controller.probe_status = MagicMock(return_value=diag)
        controller.destroy_cluster = MagicMock(
            side_effect=RuntimeError("destroy failed"))
        pod, pod_body = self.make_pod()
        pod.index = 0

        with self.assertRaisesRegex(RuntimeError, "destroy failed"):
            controller.on_pod_deleted(pod, pod_body, MagicMock())

        pod.remove_member_finalizer.assert_called_once_with(pod_body)
