targetScope = 'resourceGroup'

// Inputs supplied by Azure Developer CLI for environment-specific naming and
// the existing Azure SQL destination used by the pipeline.
@description('Short environment name used in resource names.')
@maxLength(10)
param environmentName string

@description('Azure region for the Function App resources.')
param location string = resourceGroup().location

@description('Existing Azure SQL logical server host name.')
param sqlServer string

@description('Existing Azure SQL database name.')
param sqlDatabase string

@description('Object ID of the deploying user for DTS dashboard and package upload.')
param principalId string = ''

// A stable resource-group token keeps globally unique names deterministic
// across repeated deployments of the same environment.
var token = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var functionName = 'func-geo-${environmentName}-${take(token, 6)}'
var storageName = 'stgeo${take(token, 15)}'
var deploymentContainerName = 'app-package'
var taskHubName = 'default'

// Built-in Azure role IDs are constants so role assignments remain explicit
// and do not depend on display-name lookups during deployment.
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

// Centralized monitoring: Application Insights stores telemetry in this
// workspace and requires Microsoft Entra authentication.
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-geo-${environmentName}-${take(token, 6)}'
  location: location
  properties: {
    retentionInDays: 30
    features: {
      searchVersion: 1
    }
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-geo-${environmentName}-${take(token, 6)}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: true
  }
}

// Runtime storage holds Azure Functions state and the deployment package.
// Public blobs and shared-key authentication are disabled.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
  resource blobServices 'blobServices' = {
    name: 'default'
    resource deploymentContainer 'containers' = {
      name: deploymentContainerName
      properties: {
        publicAccess: 'None'
      }
    }
  }
}

// One user-assigned identity authenticates the Function App to storage,
// monitoring, Key Vault, Durable Task Scheduler, and Azure SQL.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-geo-${environmentName}-${take(token, 6)}'
  location: location
}

// Profound credentials are referenced from Key Vault at runtime; purge
// protection prevents accidental permanent secret deletion.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-geo-${environmentName}-${take(token, 6)}'
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

// Durable Task Scheduler persists orchestration state independently of the
// Function host. The task hub name must match host.json and app settings.
resource scheduler 'Microsoft.DurableTask/schedulers@2025-11-01' = {
  name: 'dts-geo-${environmentName}-${take(token, 6)}'
  location: location
  properties: {
    sku: {
      name: 'Consumption'
    }
    ipAllowlist: [
      '0.0.0.0/0'
    ]
  }
}

resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2025-11-01' = {
  parent: scheduler
  name: taskHubName
}

// Flex Consumption provides the Linux Python 3.12 runtime used by the app.
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-geo-${environmentName}-${take(token, 6)}'
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

// The Function App performs SQL-only scheduled exports. Every external
// service connection below uses the user-assigned managed identity.
resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    keyVaultReferenceIdentity: identity.id
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
  }
  resource appSettings 'config' = {
    name: 'appsettings'
    properties: {
      FUNCTIONS_WORKER_RUNTIME: 'python'
      AzureWebJobsStorage__accountName: storage.name
      AzureWebJobsStorage__credential: 'managedidentity'
      AzureWebJobsStorage__clientId: identity.properties.clientId
      APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
      APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identity.properties.clientId};Authorization=AAD'
      AZURE_CLIENT_ID: identity.properties.clientId
      AZURE_SQL_SERVER: sqlServer
      AZURE_SQL_DATABASE: sqlDatabase
      PROFOUND_API_KEY: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=profound-api-key)'
      PIPELINE_TIMER_SCHEDULE: '0 0 * * * *'
      TASKHUB_NAME: taskHub.name
      DURABLE_TASK_SCHEDULER_CONNECTION_STRING: 'Endpoint=${scheduler.properties.endpoint};TaskHub=${taskHub.name};Authentication=ManagedIdentity;ClientID=${identity.properties.clientId}'
    }
  }
}

// Runtime identity permissions follow least privilege for each backing
// service required by Azure Functions and Durable Functions.
resource identityStorageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, storageBlobDataOwnerRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityStorageQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, storageQueueDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityStorageTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, storageTableDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(applicationInsights.id, identity.id, monitoringMetricsPublisherRoleId)
  scope: applicationInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityDurableTaskContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(scheduler.id, identity.id, durableTaskDataContributorRoleId)
  scope: scheduler
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Optional deployer permissions let azd upload packages and inspect the
// scheduler. They are omitted when no deploying principal ID is supplied.
resource deployerStorageBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(storage.id, principalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: principalId
    principalType: 'User'
  }
}

resource deployerDurableTaskContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(scheduler.id, principalId, durableTaskDataContributorRoleId)
  scope: scheduler
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalId: principalId
    principalType: 'User'
  }
}

// Azure Developer CLI consumes the service resource name; the remaining
// outputs support post-provisioning Key Vault and Azure SQL setup.
output SERVICE_PIPELINE_RESOURCE_NAME string = functionApp.name
output FUNCTION_APP_NAME string = functionApp.name
output FUNCTION_IDENTITY_NAME string = identity.name
output FUNCTION_IDENTITY_PRINCIPAL_ID string = identity.properties.principalId
output KEY_VAULT_NAME string = keyVault.name
output DURABLE_TASK_SCHEDULER_NAME string = scheduler.name
