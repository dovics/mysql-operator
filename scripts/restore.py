#!/usr/bin/env python3
"""
MySQL Backup Restore Script

Creates a Kubernetes Job to restore a MySQLBackup to an InnoDBCluster.
The job runs a Go binary that fetches all required data via client-go.

Usage:
    python restore.py --cluster <cluster> --backup-name <backup> [--namespace <ns>]
"""

import argparse
import sys
import time
from typing import Optional, Dict, Any

from kubernetes import client
from kubernetes.client.rest import ApiException
import kubernetes.config


def get_common_labels(cluster_name: str) -> Dict[str, str]:
    """Get common labels for restore resources"""
    return {
        "mysql.oracle.com/cluster": cluster_name,
        "app.kubernetes.io/name": "mysql-backup-restore",
        "app.kubernetes.io/managed-by": "mysql-operator",
        "app.kubernetes.io/created-by": "restore-script"
    }


def get_mysqlbackup(namespace: str, name: str) -> Dict[str, Any]:
    """Get MySQLBackup CR"""
    custom_api = client.CustomObjectsApi()
    try:
        return custom_api.get_namespaced_custom_object(
            group="mysql.oracle.com",
            version="v2",
            namespace=namespace,
            plural="mysqlbackups",
            name=name
        )
    except ApiException as e:
        if e.status == 404:
            print(f"Error: MySQLBackup {namespace}/{name} not found")
            sys.exit(1)
        raise


def extract_storage_config(backup: Dict[str, Any]) -> Dict[str, Any]:
    """Extract storage configuration from MySQLBackup"""
    storage_config = {
        "type": None,
        "secret_name": None,
        "bucket_name": None,
        "container_name": None
    }

    # First try to get from status (for completed backups)
    status = backup.get("status", {})
    method = status.get("method", "")

    if "s3" in method:
        storage_config["type"] = "s3"
        storage_config["bucket_name"] = status.get("bucket", "")
    elif "oci" in method or "ociObjectStorage" in method:
        storage_config["type"] = "oci"
        storage_config["bucket_name"] = status.get("bucket", "")
    elif "azure" in method:
        storage_config["type"] = "azure"
        storage_config["container_name"] = status.get("container", "")

    # Then try to get from spec for secret name (and fallback bucket/container)
    spec = backup.get("spec", {})
    backup_profile = spec.get("backupProfile", spec)  # May be nested or not

    # Check dumpInstance first
    dump_instance = backup_profile.get("dumpInstance", {})
    dump_storage = dump_instance.get("storage", {}) if dump_instance else {}

    if "s3" in dump_storage:
        storage_config["secret_name"] = dump_storage["s3"].get("config")
        if storage_config["bucket_name"] is None:
            storage_config["bucket_name"] = dump_storage["s3"].get("bucketName")
    elif "ociObjectStorage" in dump_storage:
        storage_config["secret_name"] = dump_storage["ociObjectStorage"].get("credentials")
        if storage_config["bucket_name"] is None:
            storage_config["bucket_name"] = dump_storage["ociObjectStorage"].get("bucketName")
    elif "azure" in dump_storage:
        storage_config["secret_name"] = dump_storage["azure"].get("config")
        if storage_config["container_name"] is None:
            storage_config["container_name"] = dump_storage["azure"].get("containerName")

    # Check snapshot
    snapshot = backup_profile.get("snapshot", {})
    snapshot_storage = snapshot.get("storage", {}) if snapshot else {}

    if "s3" in snapshot_storage:
        storage_config["secret_name"] = snapshot_storage["s3"].get("config")
        if storage_config["bucket_name"] is None:
            storage_config["bucket_name"] = snapshot_storage["s3"].get("bucketName")

    # Also check for backupProfileName reference by looking at the actual structure
    if storage_config["secret_name"] is None and backup_profile.get("name"):
        # Check if storage is directly under dumpInstance
        if isinstance(dump_instance, dict):
            for key, value in dump_instance.items():
                if key == "storage" and isinstance(value, dict):
                    for storage_type, storage_data in value.items():
                        if storage_type == "s3":
                            storage_config["secret_name"] = storage_data.get("config")
                            storage_config["bucket_name"] = storage_data.get("bucketName")
                        elif storage_type == "ociObjectStorage":
                            storage_config["secret_name"] = storage_data.get("credentials")
                            storage_config["bucket_name"] = storage_data.get("bucketName")
                        elif storage_type == "azure":
                            storage_config["secret_name"] = storage_data.get("config")
                            storage_config["container_name"] = storage_data.get("containerName")

    return storage_config


def extract_storage_config_from_cluster(cluster: Dict[str, Any], profile_name: str) -> Dict[str, Any]:
    """Extract storage configuration from InnoDBCluster backupProfile"""
    storage_config = {
        "type": None,
        "secret_name": None,
        "bucket_name": None,
        "container_name": None
    }

    backup_profiles = cluster.get("spec", {}).get("backupProfiles", [])
    for profile in backup_profiles:
        if profile.get("name") == profile_name:
            # Check dumpInstance
            dump_instance = profile.get("dumpInstance", {})
            dump_storage = dump_instance.get("storage", {}) if dump_instance else {}

            if "s3" in dump_storage:
                storage_config["type"] = "s3"
                storage_config["secret_name"] = dump_storage["s3"].get("config")
                storage_config["bucket_name"] = dump_storage["s3"].get("bucketName")
            elif "ociObjectStorage" in dump_storage:
                storage_config["type"] = "oci"
                storage_config["secret_name"] = dump_storage["ociObjectStorage"].get("credentials")
                storage_config["bucket_name"] = dump_storage["ociObjectStorage"].get("bucketName")
            elif "azure" in dump_storage:
                storage_config["type"] = "azure"
                storage_config["secret_name"] = dump_storage["azure"].get("config")
                storage_config["container_name"] = dump_storage["azure"].get("containerName")

            # Check snapshot if not found in dumpInstance
            if storage_config["secret_name"] is None:
                snapshot = profile.get("snapshot", {})
                snapshot_storage = snapshot.get("storage", {}) if snapshot else {}
                if "s3" in snapshot_storage:
                    storage_config["type"] = "s3"
                    storage_config["secret_name"] = snapshot_storage["s3"].get("config")
                    storage_config["bucket_name"] = snapshot_storage["s3"].get("bucketName")
            break

    return storage_config


def create_restore_job(
    namespace: str,
    job_name: str,
    backup_name: str,
    cluster_name: str,
    service_account_name: str,
    operator_image: str,
    image_pull_policy: str = "IfNotPresent"
):
    """Create restore Job - simplified, Go binary fetches all data via client-go"""
    batch_api = client.BatchV1Api()

    # Get MySQLBackup to extract storage config for credential mounting
    backup = get_mysqlbackup(namespace, backup_name)
    storage_config = extract_storage_config(backup)

    # If backup uses backupProfileName reference, fetch from cluster
    if storage_config["secret_name"] is None and backup.get("spec", {}).get("backupProfileName"):
        profile_name = backup["spec"]["backupProfileName"]
        cluster = get_innodbcluster(namespace, cluster_name)
        storage_config = extract_storage_config_from_cluster(cluster, profile_name)

    volume_mounts = [
        {
            "name": "mysqlsh-home",
            "mountPath": "/mysqlsh"
        }
    ]
    volumes = [
        {
            "name": "mysqlsh-home",
            "emptyDir": {}
        }
    ]

    # Add storage-specific credential volumes
    if storage_config["type"] == "s3" and storage_config["secret_name"]:
        volume_mounts.append({
            "name": "s3-config-volume",
            "readOnly": True,
            "mountPath": "/mysqlsh/.aws"
        })
        volumes.append({
            "name": "s3-config-volume",
            "secret": {
                "secretName": storage_config["secret_name"]
            }
        })
    elif storage_config["type"] == "azure" and storage_config["secret_name"]:
        volume_mounts.append({
            "name": "azure-config-volume",
            "readOnly": True,
            "mountPath": "/mysqlsh/.azure"
        })
        volumes.append({
            "name": "azure-config-volume",
            "secret": {
                "secretName": storage_config["secret_name"]
            }
        })
    elif storage_config["type"] == "oci" and storage_config["secret_name"]:
        volume_mounts.append({
            "name": "oci-config-volume",
            "readOnly": True,
            "mountPath": "/etc/backup/credentials"
        })
        volumes.append({
            "name": "oci-config-volume",
            "secret": {
                "secretName": storage_config["secret_name"]
            }
        })

    job_body = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": get_common_labels(cluster_name)
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": get_common_labels(cluster_name)
                },
                "spec": {
                    "serviceAccountName": service_account_name,
                    "securityContext": {
                        "runAsUser": 27,
                        "runAsGroup": 27,
                        "fsGroup": 27,
                        "runAsNonRoot": True
                    },
                    "containers": [
                        {
                            "name": "restore-backup",
                            "image": operator_image,
                            "imagePullPolicy": image_pull_policy,
                            "command": ["/opt/restore"],
                            "args": [
                                "--cluster", cluster_name,
                                "--namespace", namespace,
                                "--backup-name",backup_name
                            ],
                            "env": [
                                {
                                    "name": "HOME",
                                    "value": "/mysqlsh"
                                },
                                {
                                    "name": "MYSQLSH_USER_CONFIG_HOME",
                                    "value": "/mysqlsh"
                                }
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "privileged": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}
                            },
                            "volumeMounts": volume_mounts
                        }
                    ],
                    "volumes": volumes,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 60
                }
            }
        }
    }

    batch_api.create_namespaced_job(namespace=namespace, body=job_body)
    print(f"Job {job_name} created with {storage_config['type']} credential mounting")


def wait_for_job_completion(namespace: str, job_name: str, timeout: int = 3600):
    """Wait for Job to complete"""
    batch_api = client.BatchV1Api()
    core_api = client.CoreV1Api()

    start_time = time.time()

    print(f"Waiting for Job {job_name} to complete...")

    while time.time() - start_time < timeout:
        try:
            job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)

            if job.status.succeeded and job.status.succeeded > 0:
                print(f"Job {job_name} completed successfully!")
                return True

            if job.status.failed and job.status.failed > 0:
                print(f"Job {job_name} failed!")
                pods = core_api.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=f"job-name={job_name}"
                )
                for pod in pods.items:
                    print(f"\nLogs for Pod {pod.metadata.name}:")
                    try:
                        log = core_api.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=namespace,
                            container="restore-backup"
                        )
                        print(log)
                    except Exception as e:
                        print(f"Failed to get logs: {e}")
                return False

            time.sleep(10)
        except ApiException as e:
            print(f"Error checking job status: {e}")
            time.sleep(5)

    print(f"Timeout waiting for Job {job_name} to complete")
    return False


def get_innodbcluster(namespace: str, name: str) -> Dict[str, Any]:
    """Get InnoDBCluster CR"""
    custom_api = client.CustomObjectsApi()
    try:
        return custom_api.get_namespaced_custom_object(
            group="mysql.oracle.com",
            version="v2",
            namespace=namespace,
            plural="innodbclusters",
            name=name
        )
    except ApiException as e:
        if e.status == 404:
            print(f"Error: InnoDBCluster {namespace}/{name} not found")
            sys.exit(1)
        raise


def create_arg_parser() -> argparse.ArgumentParser:
    """Create argument parser"""
    parser = argparse.ArgumentParser(
        description="Restore a MySQLBackup to an InnoDBCluster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python restore.py --cluster mycluster --backup-name mybackup

  python restore.py --namespace myns --cluster mycluster --backup-name mybackup

  python restore.py --cluster mycluster --backup-name mybackup --dry-run

  python restore.py --cluster mycluster --backup-name mybackup --wait
        """
    )
    parser.add_argument("--namespace", "-n", default="default", help="Kubernetes namespace")
    parser.add_argument("--cluster", "-c", required=True, help="InnoDBCluster name")
    parser.add_argument("--backup-name", "-b", required=True, help="MySQLBackup name")
    parser.add_argument("--dry-run", action="store_true", help="Show config without creating resources")
    parser.add_argument("--wait", action="store_true", help="Wait for job completion")
    parser.add_argument("--wait-timeout", type=int, default=3600, help="Wait timeout in seconds")
    return parser


def main():
    parser = create_arg_parser()
    args = parser.parse_args()

    # Load kube config
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()

    # Get cluster for operator image and service account
    print(f"Fetching InnoDBCluster {args.namespace}/{args.cluster}...")
    cluster = get_innodbcluster(args.namespace, args.cluster)

    image_pull_policy = cluster.get("spec", {}).get("imagePullPolicy", "IfNotPresent")
    operator_image = "mysql-operator-restore:9.7.0"
    service_account_name = cluster.get("spec", {}).get(
        "serviceAccountName", f"{args.cluster}-sidecar-sa"
    )

    # Build job name
    job_name = f"{args.cluster[:20]}-restore-{args.backup_name[-30:]}"
    if len(job_name) > 63:
        import hashlib
        suffix = hashlib.md5(args.backup_name.encode()).hexdigest()[:8]
        job_name = f"{args.cluster[:40]}-restore-{suffix}"

    print("\n" + "=" * 60)
    print("Restore configuration:")
    print(f"  Namespace: {args.namespace}")
    print(f"  Cluster: {args.cluster}")
    print(f"  Backup: {args.backup_name}")
    print(f"  Job name: {job_name}")
    print(f"  Operator image: {operator_image}")
    print(f"  ServiceAccount: {service_account_name}")
    print("=" * 60 + "\n")

    if args.dry_run:
        print("Dry-run mode, not creating resources")
        return

    # Get storage config for debugging
    backup = get_mysqlbackup(args.namespace, args.backup_name)
    storage_config = extract_storage_config(backup)

    # If backup uses backupProfileName reference, fetch from cluster
    if storage_config["secret_name"] is None and backup.get("spec", {}).get("backupProfileName"):
        profile_name = backup["spec"]["backupProfileName"]
        print(f"Backup uses profile reference: {profile_name}, fetching from cluster...")
        cluster = get_innodbcluster(args.namespace, args.cluster)
        storage_config = extract_storage_config_from_cluster(cluster, profile_name)

    print("Storage configuration:")
    print(f"  Type: {storage_config['type']}")
    print(f"  Bucket name: {storage_config.get('bucket_name', 'N/A')}")
    print(f"  Container name: {storage_config.get('container_name', 'N/A')}")
    print(f"  Secret name: {storage_config.get('secret_name', 'N/A')}")

    # Debug: print structure if secret is missing
    if storage_config["secret_name"] is None:
        import json
        print("\nDEBUG: Backup object structure (spec):")
        print(json.dumps(backup.get("spec", {}), indent=2, default=str))
        print("\nDEBUG: Cluster backupProfiles:")
        cluster = get_innodbcluster(args.namespace, args.cluster)
        print(json.dumps(cluster.get("spec", {}).get("backupProfiles", []), indent=2, default=str))

    # Create Job - Go binary fetches all data via client-go
    print("\nCreating Job...")
    create_restore_job(
        namespace=args.namespace,
        job_name=job_name,
        backup_name=args.backup_name,
        cluster_name=args.cluster,
        service_account_name=service_account_name,
        operator_image=operator_image,
        image_pull_policy=image_pull_policy
    )

    print(f"\nRestore job created!")
    print(f"View job status: kubectl -n {args.namespace} get job {job_name}")
    print(f"View pod logs: kubectl -n {args.namespace} logs -f job-name={job_name}")

    if args.wait:
        success = wait_for_job_completion(args.namespace, job_name, args.wait_timeout)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
