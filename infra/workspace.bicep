// Everything an Azure ML workspace needs, in the group `main.bicep` just made.
//
// Names are DETERMINISTIC -- `uniqueString(subscription().id, prefix)` gives
// the same suffix every time for the same person in the same subscription.
// That is deliberate and it has a cost: a Key Vault deleted with the group is
// soft-deleted, not gone, and its name is held for 90 days, so a re-run of
// `infra up` collides with the corpse of the last one. `ffsft infra down`
// purges the vault for exactly this reason. Measured on this subscription:
// soft-delete is ON (90 days) and purge protection is OFF, so the purge is
// permitted. Where a policy turns purge protection ON, the name is locked for
// the full retention and a re-run needs a new prefix -- there is no way round
// it, which is why `infra down` reports the purge rather than assuming it.

@minLength(3)
@maxLength(8)
param prefix string

param location string

// 13 chars, stable per (subscription, prefix).
var suffix = uniqueString(subscription().id, prefix)

var storageName = 'st${prefix}${suffix}'
var keyVaultName = 'kv${prefix}${suffix}'
var registryName = 'acr${prefix}${suffix}'
var workspaceName = 'mlw-${prefix}'

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // Training data and merged weights live here. Blobs are reachable with the
    // caller's Entra identity, never anonymously.
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // `enablePurgeProtection` is OMITTED, not set false: the Key Vault API
    // accepts `true` or absence and rejects a downgrade, and Bicep emits an
    // explicit `null` for an unset property, which is a value the API can see.
    // Leaving the line out is the only way to say "do not turn this on".
    // Purge protection would hold this vault's name for the full retention
    // window with no override, and the workshop's teardown story is "delete
    // the group and run it again tomorrow".
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    // The endpoint pulls the custom vLLM image with its managed identity
    // (AcrPull), so the admin user stays off.
    adminUserEnabled: false
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-${prefix}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource workspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: workspaceName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'ffsft ${prefix}'
    description: 'Fabric + Foundry Korean sLLM fine-tuning workshop'
    storageAccount: storage.id
    keyVault: vault.id
    applicationInsights: insights.id
    // Linked here rather than left for the deploy path to discover: the
    // endpoint's identity grant reads `properties.containerRegistry` off the
    // workspace, and an empty one sent the grant to a registry in another
    // group (src/ffsft/deploy/identity.py:319).
    containerRegistry: registry.id
    publicNetworkAccess: 'Enabled'
  }
}

output workspaceName string = workspace.name
output registryName string = registry.name
output storageName string = storage.name
output keyVaultName string = vault.name
