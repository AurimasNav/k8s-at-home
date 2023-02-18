# Disclaimer

Initial idea was to use vault for secret management, but it was too complicated for this simple home setup and also creates chicken and egg situation, saas secret manager was chosen instead and vault documentation steps moved to separate file for archival purposes, if I decide to go back to it at some point.

## Prepping hashicorp vault | [doc](https://developer.hashicorp.com/vault/tutorials/kubernetes/kubernetes-minikube-raft)

- monitor argocd until `vault` application is deployed except for `vault-0` pod (is should be `running`, but in 0/1 ready state)
    
    ```sh
    ~$ kubectl get pods -n vault
    NAME                                    READY   STATUS    RESTARTS   AGE
    vault-agent-injector-59b9c84fd8-mzzj8   1/1     Running   0          43m
    vault-0                                 0/1     Running   0          28m
    ```

### Unsealing vault

- initialize vault-0 with one key share and one key threshold (for simplity sake, not secure)

    ```sh
    $ kubectl exec vault-0 -- vault operator init \
    -key-shares=1 \
    -key-threshold=1 \
    -format=json > cluster-keys.json
    ```

- display unseal key found in `cluster-jeys.json`

    ```sh
    jq -r ".unseal_keys_b64[]" cluster-keys.json
    rrUtT32GztRy/pVWmcH0ZQLCCXon/TxCgi40FL1Zzus=
    ```

- save the key into variable so it is not in shell history

    ```sh
    VAULT_UNSEAL_KEY=$(jq -r ".unseal_keys_b64[]" cluster-keys.json)
    ```

- unseal the vault on `vault-0` pod

    ```sh
    kubectl exec vault-0 -- vault operator unseal $VAULT_UNSEAL_KEY
    ```

### Set a secret in Vault

- display the root token found in `cluster-keys.json`

    ```sh
    $ jq -r ".root_token" cluster-keys.json
    hvs.HJmsajgGlWPTx6YNHoUljuOO
    ```

- start interactive shell session on tthe `vault-0` pod

    ```sh
    $ kubectl exec --stdin=true --tty=true vault-0 -n vault -- /bin/sh
    / $
    ```

- login with the root token

    ```sh
    $ vault login
    Token (will be hidden):
    Success! You are now authenticated. The token information displayed below
    is already stored in the token helper. You do NOT need to run "vault login"
    again. Future Vault requests will automatically use this token.

    Key                  Value
    ---                  -----
    token                hvs.HJmsajgGlWPTx6YNHoUljuOO
    token_accessor       JVsMJHVu6rTWbPLlYmWQTq1R
    token_duration       ∞
    token_renewable      false
    token_policies       ["root"]
    identity_policies    []
    policies             ["root"]
    ```

- enable an instance of the kv-v2 secrets engine at the path `secret`

    ```sh
    $ vault secrets enable -path=secret kv-v2
    Success! Enabled the kv-v2 secrets engine at: secret/
    ```

- create secrets for applications

    ```sh
    vault put secret/cert-manager/letsencrypt-issuer email=fake-email@outlook.com
    ```

- verify secret got created

    ```sh
    / $ vault kv get secret/cert-manager/letsencrypt-issuer
    =============== Secret Path ===============
    secret/data/cert-manager/letsencrypt-issuer

    ======= Metadata =======
    Key                Value
    ---                -----
    created_time       2023-01-29T19:31:27.783982045Z
    custom_metadata    <nil>
    deletion_time      n/a
    destroyed          false
    version            1

    ==== Data ====
    Key      Value
    ---      -----
    email    fake-email@outlook.com
    ```

### Configure Kubernetes authentication to vault

- enable kubernetes authentication method

    ```sh
    $ vault auth enable kubernetes
    Success! Enabled kubernetes auth method at: kubernetes/
    ```

- configure the Kubernetes authentication method to use the location of the Kubernetes API

    ```sh
    $ vault write auth/kubernetes/config \
        kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:$KUBERNETES_SERVICE_PORT"
    Success! Data written to: auth/kubernetes/config
    ```

- create admin policy (overkill, actual policy should only provide whats needed)

    ```sh
    $ vault policy write admin - <<EOF
    # Read system health check
    path "sys/health"
    {
    capabilities = ["read", "sudo"]
    }

    # Create and manage ACL policies broadly across Vault

    # List existing policies
    path "sys/policies/acl"
    {
    capabilities = ["list"]
    }

    # Create and manage ACL policies
    path "sys/policies/acl/*"
    {
    capabilities = ["create", "read", "update", "delete", "list", "sudo"]
    }

    # Enable and manage authentication methods broadly across Vault

    # Manage auth methods broadly across Vault
    path "auth/*"
    {
    capabilities = ["create", "read", "update", "delete", "list", "sudo"]
    }

    # Create, update, and delete auth methods
    path "sys/auth/*"
    {
    capabilities = ["create", "update", "delete", "sudo"]
    }

    # List auth methods
    path "sys/auth"
    {
    capabilities = ["read"]
    }

    # Enable and manage the key/value secrets engine at `secret/` path

    # List, create, update, and delete key/value secrets
    path "secret/*"
    {
    capabilities = ["create", "read", "update", "delete", "list", "sudo"]
    }

    # Manage secrets engines
    path "sys/mounts/*"
    {
    capabilities = ["create", "read", "update", "delete", "list", "sudo"]
    }

    # List existing secrets engines.
    path "sys/mounts"
    {
    capabilities = ["read"]
    }
    EOF

    Success! Uploaded policy: read-secrets
    ```

- create authentication role named `read-secrets` that connects the Kubernetes service accounts and `read-secrets` policy and allows listed namespaces to access this role

    ```sh
    $ vault write auth/kubernetes/role/external-secrets \
    bound_service_account_names="*" \
    bound_service_account_namespaces="*" \
    policies=admin \
    ttl=1h

    Success! Data written to: auth/kubernetes/role/read-secrets
    ```

# Vault saas alternative - akeyless, abandoned due to bugs and simpler solution with doppler

## Akeyless setup for external-secrets - WebUI method (todo: document an api way of doing this as preferred method)

- register account for [Akeyless account](https://console.akeyless.io/registration) (free for 5 clients and 2000 static secrets)

- create Auth Method
  - New API Key
  - Name `external-secrets`
  - Location `/`
  - Allowed Client IPs `<external_ip_of_k8s>/32`
  - <button name="Create Role">Save</button>
  - <button name="Create Role">Save to .CSV file</button>
  - take note where the file is saved, we will use it later `akeyless_creds.csv`

- create Access Role
  - Name `external-secrets-reader`
  - Location `/`
  - <button name="Create Role">Create Role</button>
  - Method <button name="Associate">:heavy_plus_sign: Associate</button>
    - Auth Method `/external-secrets`
  - Access Path <button name="Add">:heavy_plus_sign: Add</button> 
    - Allow acces to the following path `/k8s-at-home/*`
    - [x] Apply recursively
    - Allow the following operations:
      - [ ] Create
      - [x] Read
      - [ ] Update
      - [ ] Delete
      - [ ] List
      - [ ] Deny

- create Secrets & Keys
  - <button name="New">:heavy_plus_sign: New</button> Static Secret
    - Name `letsencrypt-email`
    - Location `/k8s-at-home`
    - Value `<replace-with-your-actual-email@domain.com>`

## Create secert for ClusterCredentialStore authentication to Akeyless api

- copy saved Akeyless creds, secret template and powershell script to the same directory somewhere outside git repo
    
    ```sh
    cp </replace-with-path-to/>akeyless_creds.csv ~
    cp ~/k8s-at-home/src/akeyless-secret.* ~
    ```

-  run pwsh script   

    ```sh
    $ pwsh
    PS> ./akeyless-secret.ps1

    Updated 'akeyless-secret.yaml' with values from 'akeyless_creds.csv'.

    PS> exit
    ```

- apply secret

    ```sh
    $ kubectl apply -f ./akeyless-secret.yaml -n external-secrets
    
    secret/akeyless-creds created
    ```

## Cloudflare tunnel for securely exposing apps

- create [cloudflare account](https://dash.cloudflare.com/sign-up)

- <button>:heavy_plus_sign: Add Site</button> (free plan)

### Progresss through DNS configuration on cloudflare

- "Review your DNS records" - scroll down and click  <button>Continue</button>

- take note of cloudflare's name servers listed in step 4

- login to your DNS provider and configure cloudflare dns servers for you domain

- finish DNS config on cloudflare side - <button>Done, check nameservers</button>

- at "Quick Start Guide" click `Finish later`

- at "Overview" click on <button>Check nameservers</button> once more

- wait until nameserver config is verified (will receive an email confirming it)

### Get started with Cloudflare Zero Trust

- on the side panel navigate to "Zero Trust"

- in zero trust page navigate to Access->Tunnels

- progress through <button>Complete setup</button> - choose free plan (will require adding payment method, but you won't be charged)

- complete purchase of the free plan for 0 dollars.

### Create a tunnel 

- download cloudflared binary

    ```sh
    wget -O cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    ```

- make it executable and login

    ```sh
    sudo chmod +x cloudlfared
    ./cloudflared login
    ```

- click on provided link and login, in the browser click on your cloudflare "site" and <button>Authorize</button> it

- create your tunnel

    ```sh
    $ ./cloudflared tunnel create my_tunnel

    ---
    Tunnel credentials written to /home/<user>/.cloudflared/ef824aef-7557-4b41-a398-4684585177ad.json. cloudflared chose this file based on where your origin certificate was found. Keep this file secret. To revoke these credentials, delete the tunnel.
    Created tunnel my_tunnel with id ef824aef-7557-4b41-a398-4684585177ad
    ```

- create doppler secret

    `CLOUDFLARE_CREDENTIALS_JSON`: `<contents of ef824aef-7557-4b41-a398-4684585177ad.json>`

### Route internet traffic to cloudflared | [doc](https://developers.cloudflare.com/cloudflare-one/tutorials/many-cfd-one-tunnel/#associate-your-tunnel-with-a-dns-record)

- Go to the Cloudflare dashboard.

- Navigate to the DNS tab.
  
- Now create a CNAMEs targeting .cfargotunnel.com. In this example, the tunnel ID is ef824aef-7557-4b41-a398-4684585177ad, so create a CNAME record specifically targeting ef824aef-7557-4b41-a398-4684585177ad.cfargotunnel.com.