// Copyright (c) 2025, Oracle and/or its affiliates.
//
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
//

package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

type RestoreConfig struct {
	BackupPath    string                 `json:"backup_path"`
	StorageType   string                 `json:"storage_type"`
	StorageConfig StorageConfig          `json:"storage_config"`
	LoadOptions   map[string]interface{} `json:"load_options"`
}

type StorageConfig struct {
	Bucket   string `json:"bucketName"`
	Endpoint string `json:"endpoint"`
	Profile  string `json:"profile"`

	// Additional fields for storage credentials secret name (not from JSON)
	Config string `json:"config"`

	// Azure-specific
	ContainerName string `json:"containerName"`

	// OCI-specific
	OciCredentials string `json:"ociCredentials"`

	// PVC-specific
	PersistentVolumeClaim string `json:"persistentVolumeClaim"`
}

func main() {
	cluster := flag.String("cluster", "", "Cluster name")
	backupName := flag.String("backup-name", "", "Backup name")
	namespace := flag.String("namespace", "default", "Namespace")
	flag.Parse()

	if *cluster == "" || *backupName == "" {
		fmt.Fprintf(os.Stderr, "Error: --cluster and --backup-name are required\n")
		os.Exit(1)
	}

	ctx := context.Background()

	// Create Kubernetes client
	k8sClient, err := NewK8sClient()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating K8s client: %v\n", err)
		os.Exit(1)
	}

	// Fetch MySQLBackup CR
	backup, err := k8sClient.GetMySQLBackup(ctx, *namespace, *backupName)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error fetching MySQLBackup: %v\n", err)
		os.Exit(1)
	}

	// Extract config from backup
	backupPath := backup.Status.Output
	storageType := detectStorageType(backup.Status.Method)

	// Handle backupProfileName reference - fetch profile from InnoDBCluster
	var clusterProfile *BackupProfileEntry
	if backup.Spec.BackupProfileName != "" {
		clusterName := backup.Spec.ClusterName
		if clusterName == "" {
			clusterName = *cluster
		}
		cluster, err := k8sClient.GetInnoDBCluster(ctx, *namespace, clusterName)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error fetching InnoDBCluster for profile reference: %v\n", err)
			os.Exit(1)
		}
		clusterProfile = findBackupProfile(cluster, backup.Spec.BackupProfileName)
		if clusterProfile == nil {
			fmt.Fprintf(os.Stderr, "Warning: backupProfileName '%s' not found in cluster '%s', using backup's embedded profile\n",
				backup.Spec.BackupProfileName, clusterName)
		}
	}

	storageConfig := extractStorageConfig(backup, clusterProfile)
	loadOptions := map[string]interface{}{
		"ignoreExistingObjects": true,
		"excludeSchemas":        []string{"mysql_innodb_cluster_metadata"},
	}

	// Get MySQL credentials
	mysqlSecret, err := k8sClient.GetSecret(ctx, *namespace, *cluster+"-mysql-secret")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error fetching MySQL secret: %v\n", err)
		os.Exit(1)
	}
	mysqlUser := string(mysqlSecret.Data["customUser"])
	mysqlPassword := string(mysqlSecret.Data["customPassword"])

	// Get storage credentials if needed
	credsSecretName := getCredentialsSecretName(storageType, storageConfig)
	if credsSecretName != "" {
		_, err = k8sClient.GetSecret(ctx, *namespace, credsSecretName)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error fetching storage credentials: %v\n", err)
			os.Exit(1)
		}
	}

	// Configure storage credentials
	if err := configureStorageCredentials(storageType, storageConfig); err != nil {
		fmt.Fprintf(os.Stderr, "Error configuring storage credentials: %v\n", err)
		os.Exit(1)
	}

	config := &RestoreConfig{
		BackupPath:    backupPath,
		StorageType:   storageType,
		StorageConfig: storageConfig,
		LoadOptions:   loadOptions,
	}

	script := generateRestoreScript(*cluster, *namespace, config, mysqlUser, mysqlPassword)

	cmd := exec.Command("mysqlsh", "--py", "-e", script)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	fmt.Printf("Executing restore: cluster=%s, backup=%s\n", *cluster, *backupName)
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Restore failed: %v\n", err)
		os.Exit(1)
	}

	fmt.Println("Restore completed successfully!")
}

func readFile(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

func configureStorageCredentials(storageType string, config StorageConfig) error {
	switch storageType {
	case "oci":
		return configureOCI(config)
	case "s3":
		return configureS3(config)
	case "azure":
		return configureAzure(config)
	}
	return nil
}

func configureOCI(config StorageConfig) error {
	// OCI credentials are mounted at /etc/backup/credentials
	// We need to write the OCI config file for mysqlsh
	credsDir := "/etc/backup/credentials"
	if _, err := os.Stat(credsDir); os.IsNotExist(err) {
		return nil
	}

	entries, err := os.ReadDir(credsDir)
	if err != nil {
		return err
	}

	var privateKey string
	creds := make(map[string]string)
	for _, entry := range entries {
		if !entry.IsDir() {
			content, err := os.ReadFile(filepath.Join(credsDir, entry.Name()))
			if err != nil {
				continue
			}
			if entry.Name() == "privatekey" {
				privateKey = string(content)
			} else {
				creds[entry.Name()] = string(content)
			}
		}
	}

	// Write OCI config
	ociConfigDir := "/mysqlsh"
	os.MkdirAll(ociConfigDir, 0755)

	configPath := "/mysqlsh/oci_config"
	configContent := "[DEFAULT]\n"
	for k, v := range creds {
		configContent += fmt.Sprintf("%s = %s\n", k, v)
	}
	configContent += "key_file = /mysqlsh/privatekey.pem\n"

	if err := os.WriteFile(configPath, []byte(configContent), 0600); err != nil {
		return err
	}

	if privateKey != "" {
		if err := os.WriteFile("/mysqlsh/privatekey.pem", []byte(privateKey), 0600); err != nil {
			return err
		}
	}

	return nil
}

func configureS3(config StorageConfig) error {
	// S3 credentials are expected to be mounted at /tmp/.aws
	// No configuration needed if mounted correctly
	return nil
}

func configureAzure(config StorageConfig) error {
	// Azure credentials are expected to be mounted at /tmp/.azure
	// No configuration needed if mounted correctly
	return nil
}

func generateRestoreScript(cluster, namespace string, config *RestoreConfig, mysqlUser, mysqlPassword string) string {
	host := fmt.Sprintf("%s-0.%s-instances.%s.svc.cluster.local:3306", cluster, cluster, namespace)

	return fmt.Sprintf(`
import os
import sys
import mysqlsh

shell = mysqlsh.globals.shell
util = mysqlsh.globals.util

# Connect to MySQL
shell.connect({
    'user': '%s',
    'password': '%s',
    'host': '%s'.split(':')[0],
    'port': 3306
})

# Prepare options
options = {"ignoreExistingObjects":True,"excludeSchemas":["mysql_innodb_cluster_metadata"]}
options['progressFile'] = ''

# Add storage-specific options
storage_type = '%s'
if storage_type == 'oci':
    options['ociConfigFile'] = '/mysqlsh/oci_config'
    options['ociProfile'] = 'DEFAULT'
    options['osBucketName'] = '%s'
elif storage_type == 's3':
    options['s3BucketName'] = '%s'
    if '%s':
        options['s3Profile'] = '%s'
    endpoint = '%s'
    if endpoint:
        options['s3EndpointOverride'] = endpoint
elif storage_type == 'azure':
    options['azureContainerName'] = '%s'

print(f"Loading dump from: %s")
print(f"Options: {options}")

util.load_dump('%s', options)
print("Restore completed successfully!")
`,
		mysqlUser,
		mysqlPassword,
		host,
		config.StorageType,
		config.StorageConfig.Bucket,
		config.StorageConfig.Bucket,
		config.StorageConfig.Profile,
		config.StorageConfig.Profile,
		config.StorageConfig.Endpoint,
		config.StorageConfig.ContainerName,
		config.BackupPath,
		config.BackupPath,
	)
}
