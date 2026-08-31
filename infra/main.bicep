// One participant, one prefix, one resource group.
//
// The workshop's teardown story is `az group delete`, and that story only
// works if everything a participant creates lands in ONE resource group.
// The repo learned this the expensive way: a polandcentral endpoint was
// pointed at `acrffsftkc`, which lives in `rg-ffsft-kc`, so deleting either
// group left the other half broken (src/ffsft/deploy/identity.py:411).
// Splitting training and serving across groups is what made "did I turn
// everything off?" unanswerable.
//
// Deployed at subscription scope because it creates the resource group too:
//   az deployment sub create -l <region> -f infra/main.bicep \
//      --parameters prefix=<prefix> location=<region>

targetScope = 'subscription'

@minLength(3)
@maxLength(8)
@description('Lowercase letters and digits. Yours alone -- it names every resource.')
param prefix string

@description('One region for everything. Pick one with GPU quota (Lab 0 checks).')
param location string

@description('Tag applied to the group so a stray `az group list` shows what this is.')
param workshop string = 'ffsft'

var rgName = 'rg-ffsft-${prefix}'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: rgName
  location: location
  tags: {
    workshop: workshop
    prefix: prefix
    // `az group delete` is the intended teardown. Saying so on the group
    // itself means the answer survives losing the terminal that made it.
    teardown: 'az group delete -n ${rgName} --yes'
  }
}

module ws 'workspace.bicep' = {
  name: 'ffsft-${prefix}-workspace'
  scope: rg
  params: {
    prefix: prefix
    location: location
  }
}

output resourceGroup string = rgName
output workspaceName string = ws.outputs.workspaceName
output registryName string = ws.outputs.registryName
output storageName string = ws.outputs.storageName
output keyVaultName string = ws.outputs.keyVaultName
