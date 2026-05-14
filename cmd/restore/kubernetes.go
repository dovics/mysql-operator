// Copyright (c) 2025, Oracle and/or its affiliates.
//
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
//

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// K8sClient wraps Kubernetes clients for restore operations
type K8sClient struct {
	clientset *kubernetes.Clientset
	dynamic   dynamic.Interface
}

// NewK8sClient creates a new Kubernetes client for restore operations
func NewK8sClient() (*K8sClient, error) {
	var config *rest.Config
	var err error

	// Try in-cluster config first
	config, err = rest.InClusterConfig()
	if err != nil {
		// Fall back to local kubeconfig
		kubeconfig := os.Getenv("KUBECONFIG")
		if kubeconfig == "" {
			home := os.Getenv("HOME")
			kubeconfig = home + "/.kube/config"
		}
		config, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("failed to get kubeconfig: %w", err)
		}
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create clientset: %w", err)
	}

	dynamicClient, err := dynamic.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create dynamic client: %w", err)
	}

	return &K8sClient{
		clientset: clientset,
		dynamic:   dynamicClient,
	}, nil
}

// GetSecret retrieves a Kubernetes Secret
func (k *K8sClient) GetSecret(ctx context.Context, namespace, name string) (*corev1.Secret, error) {
	return k.clientset.CoreV1().Secrets(namespace).Get(ctx, name, metav1.GetOptions{})
}

// MySQLBackup represents the MySQLBackup CR
type MySQLBackup struct {
	Status MySQLBackupStatus `json:"status"`
	Spec   MySQLBackupSpec   `json:"spec"`
}

// MySQLBackupStatus contains status information from the MySQLBackup CR
type MySQLBackupStatus struct {
	Output    string `json:"output"`
	Method    string `json:"method"`
	Bucket    string `json:"bucket"`
	Container string `json:"container"`
}

// MySQLBackupSpec contains spec information from the MySQLBackup CR
type MySQLBackupSpec struct {
	BackupProfile     BackupProfile `json:"backupProfile"`
	BackupProfileName string        `json:"backupProfileName"`
	ClusterName       string        `json:"clusterName"`
}

// InnoDBCluster represents the InnoDBCluster CR
type InnoDBCluster struct {
	Spec InnoDBClusterSpec `json:"spec"`
}

// InnoDBClusterSpec contains spec information from the InnoDBCluster CR
type InnoDBClusterSpec struct {
	BackupProfiles []BackupProfileEntry `json:"backupProfiles"`
}

// BackupProfileEntry represents a backup profile entry in the InnoDBCluster spec
type BackupProfileEntry struct {
	Name         string        `json:"name"`
	DumpInstance DumpInstance  `json:"dumpInstance"`
	Snapshot     Snapshot      `json:"snapshot"`
}

// BackupProfile contains backup profile configuration
type BackupProfile struct {
	DumpInstance DumpInstance `json:"dumpInstance"`
	Snapshot     Snapshot     `json:"snapshot"`
}

// DumpInstance represents dump instance storage configuration
type DumpInstance struct {
	Storage map[string]StorageConfig `json:"storage"`
}

// Snapshot represents snapshot storage configuration
type Snapshot struct {
	Storage map[string]StorageConfig `json:"storage"`
}

// S3StorageConfig represents S3 storage configuration
type S3StorageConfig struct {
	Bucket   string `json:"bucketName"`
	Endpoint string `json:"endpoint"`
	Profile  string `json:"profile"`
	Config   string `json:"config"`
}

// OCIStorageConfig represents OCI storage configuration
type OCIStorageConfig struct {
	Bucket         string `json:"bucketName"`
	OciCredentials string `json:"credentials"`
}

// AzureStorageConfig represents Azure storage configuration
type AzureStorageConfig struct {
	ContainerName string `json:"containerName"`
	Config        string `json:"config"`
}

// PVCStorageConfig represents PVC storage configuration
type PVCStorageConfig struct {
	PersistentVolumeClaim string `json:"persistentVolumeClaim"`
}

// detectStorageType parses the method URI to extract storage type
func detectStorageType(method string) string {
	if method == "" {
		return "unknown"
	}
	if containsString(method, "/s3") {
		return "s3"
	}
	if containsString(method, "/oci") {
		return "oci"
	}
	if containsString(method, "/azure") {
		return "azure"
	}
	if containsString(method, "/pvc") {
		return "pvc"
	}
	return "unknown"
}

func containsString(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

// GetMySQLBackup retrieves the MySQLBackup CR
func (k *K8sClient) GetMySQLBackup(ctx context.Context, namespace, name string) (*MySQLBackup, error) {
	gvr := schema.GroupVersionResource{
		Group:    "mysql.oracle.com",
		Version:  "v2",
		Resource: "mysqlbackups",
	}

	data, err := k.dynamic.Resource(gvr).Namespace(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to get MySQLBackup: %w", err)
	}

	jsonData, err := json.Marshal(data)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal MySQLBackup: %w", err)
	}

	var backup MySQLBackup
	if err := json.Unmarshal(jsonData, &backup); err != nil {
		return nil, fmt.Errorf("failed to unmarshal MySQLBackup: %w", err)
	}

	return &backup, nil
}

// GetInnoDBCluster retrieves the InnoDBCluster CR
func (k *K8sClient) GetInnoDBCluster(ctx context.Context, namespace, name string) (*InnoDBCluster, error) {
	gvr := schema.GroupVersionResource{
		Group:    "mysql.oracle.com",
		Version:  "v2",
		Resource: "innodbclusters",
	}

	data, err := k.dynamic.Resource(gvr).Namespace(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to get InnoDBCluster: %w", err)
	}

	jsonData, err := json.Marshal(data)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal InnoDBCluster: %w", err)
	}

	var cluster InnoDBCluster
	if err := json.Unmarshal(jsonData, &cluster); err != nil {
		return nil, fmt.Errorf("failed to unmarshal InnoDBCluster: %w", err)
	}

	return &cluster, nil
}

// findBackupProfile finds a backup profile by name in the cluster's backupProfiles list
func findBackupProfile(cluster *InnoDBCluster, profileName string) *BackupProfileEntry {
	for _, profile := range cluster.Spec.BackupProfiles {
		if profile.Name == profileName {
			return &profile
		}
	}
	return nil
}

// extractStorageConfig extracts storage configuration from MySQLBackup CR
// If the backup uses backupProfileName, pass the corresponding cluster profile as well
func extractStorageConfig(backup *MySQLBackup, clusterProfile *BackupProfileEntry) StorageConfig {
	storage := StorageConfig{}

	// Status contains the actually used bucket/container for completed backups
	if backup.Status.Bucket != "" {
		storage.Bucket = backup.Status.Bucket
	}
	if backup.Status.Container != "" {
		storage.ContainerName = backup.Status.Container
	}

	// First check: if we have a cluster profile from backupProfileName, use that
	if clusterProfile != nil {
		// Check dumpInstance storage from cluster profile
		if clusterProfile.DumpInstance.Storage != nil {
			if s3, ok := clusterProfile.DumpInstance.Storage["s3"]; ok {
				if storage.Bucket == "" {
					storage.Bucket = s3.Bucket
				}
				storage.Endpoint = s3.Endpoint
				storage.Profile = s3.Profile
				storage.Config = s3.Config
			} else if oci, ok := clusterProfile.DumpInstance.Storage["ociObjectStorage"]; ok {
				if storage.Bucket == "" {
					storage.Bucket = oci.Bucket
				}
				storage.Config = oci.OciCredentials
			} else if azure, ok := clusterProfile.DumpInstance.Storage["azure"]; ok {
				if storage.ContainerName == "" {
					storage.ContainerName = azure.ContainerName
				}
				storage.Config = azure.Config
			} else if pvc, ok := clusterProfile.DumpInstance.Storage["persistentVolumeClaim"]; ok {
				storage.PersistentVolumeClaim = pvc.PersistentVolumeClaim
			}
		}

		// Check snapshot storage from cluster profile (only S3 is relevant)
		if clusterProfile.Snapshot.Storage != nil {
			if s3, ok := clusterProfile.Snapshot.Storage["s3"]; ok {
				if storage.Bucket == "" {
					storage.Bucket = s3.Bucket
				}
				storage.Endpoint = s3.Endpoint
				storage.Profile = s3.Profile
				storage.Config = s3.Config
			}
		}

		return storage
	}

	// Fallback: check backup's own embedded backupProfile
	if backup.Spec.BackupProfile.DumpInstance.Storage != nil {
		if s3, ok := backup.Spec.BackupProfile.DumpInstance.Storage["s3"]; ok {
			if storage.Bucket == "" {
				storage.Bucket = s3.Bucket
			}
			storage.Endpoint = s3.Endpoint
			storage.Profile = s3.Profile
			storage.Config = s3.Config
		} else if oci, ok := backup.Spec.BackupProfile.DumpInstance.Storage["ociObjectStorage"]; ok {
			if storage.Bucket == "" {
				storage.Bucket = oci.Bucket
			}
			storage.Config = oci.OciCredentials
		} else if azure, ok := backup.Spec.BackupProfile.DumpInstance.Storage["azure"]; ok {
			if storage.ContainerName == "" {
				storage.ContainerName = azure.ContainerName
			}
			storage.Config = azure.Config
		} else if pvc, ok := backup.Spec.BackupProfile.DumpInstance.Storage["persistentVolumeClaim"]; ok {
			storage.PersistentVolumeClaim = pvc.PersistentVolumeClaim
		}
	}

	// Check snapshot storage (only S3 is relevant)
	if backup.Spec.BackupProfile.Snapshot.Storage != nil {
		if s3, ok := backup.Spec.BackupProfile.Snapshot.Storage["s3"]; ok {
			if storage.Bucket == "" {
				storage.Bucket = s3.Bucket
			}
			storage.Endpoint = s3.Endpoint
			storage.Profile = s3.Profile
			storage.Config = s3.Config
		}
	}

	return storage
}

// getCredentialsSecretName returns the secret name for storage credentials
func getCredentialsSecretName(storageType string, config StorageConfig) string {
	switch storageType {
	case "s3":
		return config.Config
	case "azure":
		return config.Config
	case "oci":
		return config.OciCredentials
	}
	return ""
}
