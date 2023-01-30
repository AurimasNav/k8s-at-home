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