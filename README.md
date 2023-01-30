- [Setup k8s-at-home on Ubuntu 22.04](#setup-k8s-at-home-on-ubuntu-2204)
  - [Disclaimer](#disclaimer)
  - [Uninstall k3s](#uninstall-k3s)
  - [Setup k3s on hosting machine (further referred as k3s\_host)](#setup-k3s-on-hosting-machine-further-referred-as-k3s_host)
  - [Setup tools on management host (tested on ubuntu 22.04 wsl)](#setup-tools-on-management-host-tested-on-ubuntu-2204-wsl)
  - [Install helm](#install-helm)
  - [Install argo-cd](#install-argo-cd)
    - [Deploy argo-cd applications](#deploy-argo-cd-applications)
  - [Akeyless setup for external-secrets - WebUI method (todo: document an api way of doing this as preferred method)](#akeyless-setup-for-external-secrets---webui-method-todo-document-an-api-way-of-doing-this-as-preferred-method)
  - [Create secert for ClusterCredentialStore authentication to Akeyless api](#create-secert-for-clustercredentialstore-authentication-to-akeyless-api)


# Setup k8s-at-home on Ubuntu 22.04

## Disclaimer

- this is for learning first and getting something useful later
- security and HA are not in scope for this exercise 

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

## Install argo-cd

- create namespace for argocd

    ```sh
    kubectl create namespace argocd
    ```

- clone k8s-at-home repository

    ```sh
    git clone https://github.com/AurimasNav/k8s-at-home.git ~/k8s-at-home
    ```

- deploy resources from kustomization file

    ```sh
    kustomize build ~/k8s-at-home/gitops/argocd | kubectl apply -f -
    ```

- verify that all pods are ready/running

    ```sh
    watch kubectl get pods -n argocd
    ```

### Deploy argo-cd applications

- modify flannel network to contain podcidr if not default

    ```sh
    podcidr=$(kubectl get nodes -o jsonpath='{.items[*].spec.podCIDR}')
    sed -i "s/10.244.0.0\/16/${podcidr/\//\\/}/g" ~/k8s-at-home/gitops/flannel/patch.configmap.yaml
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
