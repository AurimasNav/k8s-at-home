# Disclaimer

- this is for learning first and getting something useful later

# k3s setup on Ubuntu 22.04

- install k3s

    ```sh
    curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 644
    # Check for Ready node, takes ~30 seconds 
    k3s kubectl get node 
    ```

# Install kubectx + kubens

- install kubectx

    ```sh
    sudo snap install kubectx --classic
    ```

- copy k3s configuration to your home so kubens/kubectx can read it

    ```sh
    cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
    ```

- modify permissions on the config file

    ```sh
    chmod 600 ~/.kube/config
    ```

- verify that `kubens` command works

    ```sh
    $ kubens
    default
    kube-system
    kube-public
    kube-node-lease
    ```

# Install helm

- add apt repository and install helm

    ```sh
    curl https://baltocdn.com/helm/signing.asc | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
    sudo apt-get install apt-transport-https --yes
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
      status: deployed
      updated: 2023-01-05 19:13:03.869148967 +0000 UTC
    ```

# Install kustomize

- install kustomize with snap

    ```sh
    sudo snap install kustomize
    ```

- verify installation    

    ```sh
    $ kustomize version --short
    {kustomize/v4.5.7  2022-08-02T16:35:54Z  }
    ```

# Install argo-cd

- create kustomization file and add argo-cd's `install.yaml` to it

    ```sh
    kustomize create
    kustomize edit set namespace argocd
    
    ```