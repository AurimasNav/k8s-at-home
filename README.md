- [Setup k8s-at-home on Ubuntu 22.04](#setup-k8s-at-home-on-ubuntu-2204)
  - [Disclaimer](#disclaimer)
  - [DNS records used in this setup](#dns-records-used-in-this-setup)
  - [Fork and clone](#fork-and-clone)
  - [Uninstall k3s](#uninstall-k3s)
  - [Setup k3s on hosting machine (further referred as k3s\_host)](#setup-k3s-on-hosting-machine-further-referred-as-k3s_host)
  - [Setup tools on management host (tested on ubuntu 22.04 wsl)](#setup-tools-on-management-host-tested-on-ubuntu-2204-wsl)
  - [Install helm](#install-helm)
  - [Setup directory structure for media](#setup-directory-structure-for-media)
  - [Secret store setup - Doppler](#secret-store-setup---doppler)
    - [Setup saas part](#setup-saas-part)
    - [Prepare external-secrets for doppler secret store](#prepare-external-secrets-for-doppler-secret-store)
  - [OAuth with google](#oauth-with-google)
  - [Cloudflare tunnel for securely exposing apps](#cloudflare-tunnel-for-securely-exposing-apps)
    - [Progresss through DNS configuration on cloudflare](#progresss-through-dns-configuration-on-cloudflare)
    - [Get started with Cloudflare Zero Trust](#get-started-with-cloudflare-zero-trust)
    - [Create a tunnel](#create-a-tunnel)
  - [Install argo-cd](#install-argo-cd)
    - [Deploy argo-cd applications](#deploy-argo-cd-applications)
    - [Update argo-cd password](#update-argo-cd-password)
  - [Pi-Hole / Unbound config](#pi-hole--unbound-config)
  - [Alternative UI for qBittorrent | Mobile friendly iQbit](#alternative-ui-for-qbittorrent--mobile-friendly-iqbit)


# Setup k8s-at-home on Ubuntu 22.04

## Disclaimer

- this is for learning first and getting something useful later
- security and HA are not in scope for this exercise 

## DNS records used in this setup

- sync.lt - all other entries are CNAME records pointing to this parent domain
- login
- prowlarr
- sonarr
- radarr
- qbittorrent

## Fork and clone

- if you want to re-use this for you domain, fork it and replace all references to sync.lt domain to your own domain

- clone k8s-at-home repository

    ```sh
    git clone https://github.com/AurimasNav/k8s-at-home.git ~/k8s-at-home
    ```

## Uninstall k3s

- during setup one might find himself wanting to start over

    ```sh
    /usr/local/bin/k3s-uninstall.sh
    ```

## Setup k3s on hosting machine (further referred as k3s_host)

- install k3s

    ```sh
    curl -sfL https://get.k3s.io | sh -s - --disable=servicelb --disable=traefik --write-kubeconfig-mode 644
    # Check for Ready node, takes ~30 seconds 
    k3s kubectl get node 
    ```

## Setup tools on management host (tested on ubuntu 22.04 wsl)

- install powershell | [doc](https://learn.microsoft.com/en-us/powershell/scripting/install/install-ubuntu?view=powershell-7.3#installation-via-package-repository)

    ```sh
    sudo apt-get update
    sudo apt-get install -y wget apt-transport-https software-properties-common
    wget -q "https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/packages-microsoft-prod.deb"
    sudo dpkg -i packages-microsoft-prod.deb
    sudo apt-get update
    sudo apt-get install -y powershell
    ```

    - verify
    
    ```sh
    $ pwsh -version
    PowerShell 7.3.1
    ```

- install jq

    ```sh
    sudo apt install jq
    ```

- copy kubeconfig (run command from management host)

    ```sh
    scp <remote_user>@<k3s_host>:/etc/rancher/k3s/k3s.yaml ~/.kube/config
    ```

- modify permissions on the config file

    ```sh
    sudo chmod 600 ~/.kube/config
    ```

- replace k3s ip in `config` from 127.0.0.1 to `k3s_host` ip

    ```sh
    sed -i 's/127.0.0.1/<k3s_host_ip>/' .kube/config
    ```

- install kubectl | [doc](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
    
    ```sh
    sudo apt-get install -y ca-certificates curl
    sudo curl -fsSLo /etc/apt/keyrings/kubernetes-archive-keyring.gpg https://packages.cloud.google.com/apt/doc/apt-key.gpg
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes-archive-keyring.gpg] https://apt.kubernetes.io/ kubernetes-xenial main" | sudo tee /etc/apt/sources.list.d/kubernetes.list
    sudo apt-get update
    sudo apt-get install -y kubectl
    ```
    
    - verify
    
    ```sh
    $ kubectl version -o yaml
    clientVersion:
        buildDate: "2022-12-08T19:58:30Z"
        compiler: gc
        gitCommit: b46a3f887ca979b1a5d14fd39cb1af43e7e5d12d
        gitTreeState: clean
        gitVersion: v1.26.0
        goVersion: go1.19.4
        major: "1"
        minor: "26"
        platform: linux/amd64
    kustomizeVersion: v4.5.7
    serverVersion:
        buildDate: "2022-12-21T00:06:36Z"
        compiler: gc
        gitCommit: 48e5d2af5bfc69db051d46b6c6b83c46d15a9da5
        gitTreeState: clean
        gitVersion: v1.25.5+k3s1
        goVersion: go1.19.4
        major: "1"
        minor: "25"
        platform: linux/amd64
    ```

- install kubectx
 
    ```sh
    echo "deb [trusted=yes] http://ftp.de.debian.org/debian buster main" | sudo tee -a /etc/apt/sources.list
    sudo apt-get update
    sudo apt install kubectx
    ```

    - verify

    ```sh
    $ kubectx
    default
    ```

- verify that `kubens` command works

    ```sh
    $ kubens
    default
    kube-system
    kube-public
    kube-node-lease
    ```

## Install helm

- add apt repository and install helm

    ```sh
    curl https://baltocdn.com/helm/signing.asc | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/helm.gpg] https://baltocdn.com/helm/stable/debian/ all main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
    sudo apt-get update
    sudo apt-get install helm
    ```

- verify helm is working

    ```sh
    $ helm list -A -o yaml
    - app_version: v2.9.4
      chart: traefik-20.3.1+up20.3.0
      name: traefik
      namespace: kube-system
      revision: "1"
      status: deployed
      updated: 2023-01-05 19:13:06.321207336 +0000 UTC
    - app_version: v2.9.4
      chart: traefik-crd-20.3.1+up20.3.0
      name: traefik-crd
      namespace: kube-system
      revision: "1"
      status: deployedTH

- install kustomize | [doc](https://kubectl.docs.kubernetes.io/installation/kustomize/binaries/)

    ```sh
    cd /usr/local/bin
    curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh"  | sudo bash
    ```

- verify installation

    ```sh
    $ kustomize version --short
    {kustomize/v4.5.7  2022-08-02T16:35:54Z  }
    ```

## Setup directory structure for media

- create directories on `k3s_host` to map to sonarr/radarr etc.

    ```sh
    sudo mkdir -p /data/torrents/movies /data/torrents/tv \
    /data/media/movies /data/media/tv
    ```

- this assumes that your storage volume is mounted to /data

## Secret store setup - Doppler

### Setup saas part

- create account https://www.doppler.com/

- create workspace - k8s-at-home

- create project - k8s-at-home

- from Production env navigate to *ACCESS* and generate service token named `external-secrets` - save the generated token.

- add secrets to Production environment
  - key: `PLEX_CLAIM` value: `<claimToken>` (can be obtained from [https://www.plex.tv/claim](https://www.plex.tv/claim))

### Prepare external-secrets for doppler secret store

- update doppler secret template

    ```sh
    cp ~/k8s-at-home/src/doppler-secret.yaml ~
    read -p "Enter doppler service token: " SERVICE_TOKEN

    $ Enter doppler service token: <you_service_token_here>

    sed -i 's/dopler-service-token/'"$(echo $SERVICE_TOKEN|base64)"/ ~/doppler-secret.yaml
    ```

- create secret

    ```sh
    kubectl create namespace external-secrets
    kubectl apply -f ~/doppler-secret.yaml -n external-secrets
    ```

## OAuth with google

- Follow [Google Auth Provider](https://oauth2-proxy.github.io/oauth2-proxy/docs/configuration/oauth_provider#google-auth-provider) instructions (steps 1-7)

- generate oauth2-proxy cookie secret

    ```sh
    dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 | tr -d -- '\n' | tr -- '+/' '-_'; echo
    ```

- add secrets for OAuth to Doppler secret store
    - `GOOGLE_OAUTH_CLIENT_ID`: `<client_id_value>` (from Google Auth Provider step)
    - `GOOGLE_OAUTH_CLIENT_SECRET`: `<client_secret_value>` (from Google Auth Provider step)
    - `OAUTH2_PROXY_COOKIE_SECRET`: `<value_from_previous_step>`
    - `AUTHENTICATED_EMAILS`: `email1@gmail.com` (list of allowed emails to login - one per line)

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
    $ ./cloudflared tunnel create <tunnel_name>

    ---
    Tunnel credentials written to /home/<user>/.cloudflared/<guid>.json. cloudflared chose this file based on where your origin certificate was found. Keep this file secret. To revoke these credentials, delete the tunnel.
    Created tunnel <tunnel_name> with id <guid>
    ```

- create doppler secret

    `CLOUDFLARE_CREDENTIALS_JSON`: `<contents_of_guid.json_file>`

## Install argo-cd

- create namespace for argocd

    ```sh
    kubectl create namespace argocd
    ```

- deploy resources from kustomization file

    ```sh
    kustomize build ~/k8s-at-home/gitops/argocd | kubectl apply -f -
    ```

- verify that all pods are ready/running

    ```sh
    watch kubectl get pods -n argocd
    ```
- ignore the errors:

    these will be taken care of automatically once argocd deploys missing dependencies

    ```log
    resource mapping not found for name: "argocd" namespace: "argocd" from "STDIN": no matches for kind "ExternalSecret" in version "external-secrets.io/v1beta1"
    ensure CRDs are installed first
    Error from server (BadRequest): error when creating "STDIN": Service in version "v1" cannot be handled as a Service: strict decoding error: unknown field "spec.loadBalancerIp
    ```

### Deploy argo-cd applications

- modify flannel network to contain podcidr if not default

    ```sh
    podcidr=$(kubectl get nodes -o jsonpath='{.items[*].spec.podCIDR}')
    sed -i -r "s/\b([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}\b/${podcidr/\//\\/}/g" ~/k8s-at-home/gitops/flannel/patch.configmap.yaml
    ```

- update unbound config to `allow_snoop` from kubernetes interal address range (allows blocky to use unbound as upstream)

    ```sh
    sed -i -r "s/\b([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}\b/${podcidr/\//\\/}/g" ~/k8s-at-home/gitops/unbound/configmap.yaml
    ```

- commit and push changes back to remote

    ```sh
    git add .
    git commit -m "update kubernetes pod cidr references"
    git push
    ```

- create root application

    ```sh
    kubectl apply -f ~/k8s-at-home/argocd-apps/base/root-app.yaml
    ```

- get inital argo-cd admin password
    
    ```sh
    kubectl get secret/argocd-initial-admin-secret -n argocd -ojsonpath="{.data.password}" | base64 -d
    ```
- login to argocd and verify you can see deployments

    ```sh
    kubectl port-forward service/argocd-server -n argocd 8080:80
    ```
    - open browser and navigate to http://localhost:8080
    - login with `admin` and password obtained earlier

### Update argo-cd password

- verify that external-secrets is up and running
  
- encrypt your password with bcrypt (use sdk of your preferred language or do it [online](https://bcrypt.online/))

- add generated bcrypt hash to doppler secrets
  - `ARGOCD_ADMIN_PASSWORD`: `<your bcrypt hash>`

## Pi-Hole / Unbound config

- add pihole webui password to doppler secrets
  - `PIHOLE_WEBPASSWORD`: `<your pihole admin password>`



## Alternative UI for qBittorrent | [Mobile friendly iQbit](https://github.com/ntoporcov/iQbit)

- get pvc name (ssh to k3s_host)

    ```sh
    VOL = $(kubectl get pvc qbittorrent-config -ojsonpath="{.spec.volumeName}")
    cd /opt/local-path-provisioner/${VOL}_qbittorrent_qbittorrent-config
    git clone https://github.com/ntoporcov/iQbit.git
    ```

- change qbittorent setttings to use alternative webUI
  - login to qbittorent webui
  - go to options (gear icon)
  - navigate to Web UI tab
  - [x] Use alternative Web UI
  - Files location: `/config/iQbit/release`
