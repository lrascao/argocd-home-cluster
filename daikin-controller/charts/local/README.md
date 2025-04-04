# Create a sealed secret

```bash
kubectl create secret generic daikin-credentials --dry-run=client --from-literal=client_secret=<secret> -o yaml | kubeseal > daikin-credentials-sealed.yaml
```
