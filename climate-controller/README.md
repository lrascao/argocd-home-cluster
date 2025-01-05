# Authorize

1. Generate login link

```bash
CLIENT_ID=FhaCOHCySpLJFHGbEMb7pFGb && curl -v "https://idp.onecta.daikineurope.com/v1/oidc/authorize?response_type=code&client_id="$CLIENT_ID"&scope=openid%20onecta:basic.integration&redirect_uri=https://daikin.88288338.xyz"
```

2. Click location link
3. Get redirected to
https://daikin.88288338.xyz/?code=<code>

4. Controller captures code

CLIENT_ID=FhaCOHCySpLJFHGbEMb7pFGb CLIENT_SECRET=MleeIO2iHkDWSrjq5fjJ38MqLap1nG0LKOppgX939EMkX65ncpBXDp7S-GL0H2JDz2HOTIBwXwFMIJhQ6rTn3w && curl -X POST 'https://idp.onecta.daikineurope.com/v1/oidc/token?grant_type=authorization_code&client_id=<client-id>&client_secret=<client-secret>&code=<code>&redirect_uri=https://daikin.88288338.xyz'

# Get Info
curl https://api.onecta.daikineurope.com/v1/info --header "Authorization: Bearer <access-token>"
