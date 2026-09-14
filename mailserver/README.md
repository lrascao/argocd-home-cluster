# mailserver

Replaces the `mailforwarder` app. That one was `zixia/simple-mail-forwarder`:
a *forwarder* with no mailboxes, no IMAP and no submission — it could never
become a real mail server. This is [docker-mailserver][dms] (postfix + dovecot
+ opendkim), which keeps mail in real mailboxes on Longhorn **and** forwards a
copy to Gmail.

[dms]: https://docker-mailserver.github.io/docker-mailserver/latest/

## Shape

```
internet ──► MX mail.88288338.xyz (89.115.22.154)
             │
             ├─ :25  router DNAT rule 130 ─► VIP .40:9025 ─► haproxy ─► :30025 ─┐
             └─ :587 router DNAT rule 131 ─► VIP .40:9587 ─► haproxy ─► :30587 ─┤
                                                                                ▼
                                                                          mailserver pod
                                                        ┌───────────────────────┴───────────────┐
                                     local mailbox (Longhorn PVC)          copy ──SRS──► Gmail
                                            ▲                                              │
                                    IMAPS :993 (LAN/VPN only)                      you read here
                                                                                           │
                                   reply "as luis@88288338.xyz" ──► :587 ──► Brevo ──► world
```

Why it is built this way:

- **Outbound must go through Brevo.** The WAN IP's PTR is
  `154.22.115.89.rev.vodafone.pt` — generic consumer rDNS on a range that sits
  on Spamhaus PBL by default, and Vodafone will not set a custom PTR. Direct
  delivery from home is permanently off the table. This is normal for
  self-hosted mail and costs you nothing: the mailboxes, the DKIM key and the
  policy are still yours.
- **`:25` carries no SMTP AUTH.** DMS gets this right by default (auth is
  enabled only on the submission listeners). The old forwarder had AUTH on
  public `:25` with zero legitimate users, and absorbed 7,927 brute-force
  attempts in 5.5 days as a result.
- **`:993` is not exposed to the internet.** No router rule — reachable on the
  LAN and over WireGuard/Tailscale. Gmail does not need it, because mail gets
  to Gmail by *forwarding*, not POP3 fetch.
- **`ENABLE_SRS=1`.** Forwarding a message to Gmail means re-sending someone
  else's mail from your IP, which fails SPF and gets spam-filed. SRS rewrites
  the envelope sender so the forwarded copy passes.

## Secrets you must create (they are not in git)

Three SealedSecrets. `mailserver-tls` is the fourth secret the Deployment
needs, but cert-manager creates that one on its own.

A SealedSecret is encrypted **for one namespace + one name**. The `-n mailserver`
and the secret name in each command below must match what `deployment.yaml`
expects exactly, or the controller will refuse to unseal it.

### Before you start

```sh
# kubeseal resolves the kubeconfig at call time -- a RELATIVE path breaks the
# moment you cd, with a confusing "no configuration has been provided" error.
export KUBECONFIG=~/Projects/ansible-local-network/admin.kubeconfig
cd ~/Projects/argocd-home-cluster

# do not leave plaintext lying around in a shared temp dir
umask 077
```

Verified working: kubeseal v0.36.6 against the v0.37.0 controller
(`sealed-secrets-controller` in `kube-system`, which is what kubeseal defaults
to — no `--controller-name` needed).

### 1. `mailserver-secrets` — Brevo relay credentials + SRS key

`RELAY_PASSWORD` is a **Brevo SMTP key**, not your account password: Brevo →
SMTP & API → SMTP. `SRS_SECRET` must be stable — if DMS generates one it
changes on every restart, and bounces to already-forwarded mail stop routing.

Read interactively so nothing lands in shell history:

```sh
printf 'Brevo login: ';   read -r BREVO_USER
printf 'Brevo SMTP key: '; stty -echo; read -r BREVO_KEY; stty echo; echo

kubectl -n mailserver create secret generic mailserver-secrets \
  --from-literal=RELAY_USER="$BREVO_USER" \
  --from-literal=RELAY_PASSWORD="$BREVO_KEY" \
  --from-literal=SRS_SECRET="$(openssl rand -hex 24)" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml > mailserver/secret-relay-sealed.yaml

unset BREVO_USER BREVO_KEY
```

### 2. `mailserver-accounts` — the mailbox

This password is what Gmail's "Send mail as" authenticates with, and what a
phone or Thunderbird would use over IMAPS.

```sh
printf 'mailbox password: '; stty -echo; read -r PW; stty echo; echo
HASH="{SHA512-CRYPT}$(openssl passwd -6 "$PW")"
unset PW

# a real file, not --from-literal: the trailing newline matters, because DMS
# parses this with a `while read` loop that would drop an unterminated last line
TMP=$(mktemp)
printf 'luis@88288338.xyz|%s\n' "$HASH" > "$TMP"

kubectl -n mailserver create secret generic mailserver-accounts \
  --from-file=postfix-accounts.cf="$TMP" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml > mailserver/secret-accounts-sealed.yaml

rm -P "$TMP"; unset HASH     # -P overwrites; macOS has no shred(1)
```

`openssl passwd -6` emits `$6$salt$hash`, which is the same SHA512-CRYPT that
`doveadm pw -s SHA512-CRYPT` produces — it just does not add the
`{SHA512-CRYPT}` prefix, so the command above prepends it.

### 3. `mailserver-dkim` — a NEW signing key

The old `default` selector's private key never existed in the cluster: the
ConfigMap shipped only the public TXT record, key generation failed against the
read-only mount, and opendkim ran unsigned for two years. There is nothing to
migrate — generate fresh under selector `mail`.

Do this one FIRST of the three: its public half has to go into `config.yaml`
and be published in DNS before cutover.

```sh
WORK=$(mktemp -d); pushd "$WORK" >/dev/null

openssl genrsa -out mail.private 2048
openssl rsa -in mail.private -pubout -outform PEM \
  | grep -v '^-----' | tr -d '\n' > mail.pub

printf 'mail._domainkey.88288338.xyz 88288338.xyz:mail:/tmp/docker-mailserver/opendkim/keys/88288338.xyz/mail.private\n' > KeyTable
printf '*@88288338.xyz mail._domainkey.88288338.xyz\n' > SigningTable
printf '127.0.0.1\nlocalhost\n88288338.xyz\n' > TrustedHosts

kubectl -n mailserver create secret generic mailserver-dkim \
  --from-file=mail.private --from-file=KeyTable \
  --from-file=SigningTable --from-file=TrustedHosts \
  --dry-run=client -o yaml \
  | kubeseal --format yaml \
  > ~/Projects/argocd-home-cluster/mailserver/secret-dkim-sealed.yaml

echo; echo "=== paste this into config.yaml as mail.dkim.public_key ==="
cat mail.pub; echo

popd >/dev/null
rm -rf "$WORK"        # the private key must not survive outside the SealedSecret
```

The zone template wraps that value itself, so `config.yaml` takes the bare
base64 blob with no `v=DKIM1` prefix and no quotes.

### Check before committing

```sh
grep -L "encryptedData" mailserver/secret-*-sealed.yaml   # should print nothing
grep -c "SealedSecret" mailserver/secret-*-sealed.yaml    # 1 each
```

SealedSecrets are safe to commit — that is the whole point; only the in-cluster
controller holds the private key. Never commit the plaintext inputs.

## DNS to publish

Set these before cutting over. SPF and DKIM live in
`ansible-local-network:playbooks/coredns/templates/db.external.j2`.

| record | value |
| --- | --- |
| `mail._domainkey.88288338.xyz` TXT | `v=DKIM1; k=rsa; p=<mail.pub>` |
| `88288338.xyz` TXT (SPF) | `v=spf1 mx include:spf.brevo.com -all` |
| `_dmarc.88288338.xyz` TXT | keep `p=quarantine` until verified, then `p=reject` |

The old SPF was `v=spf1 +mx a:88288338.xyz/28 ~all`, which authorised neither
Brevo (so every relayed message failed SPF) nor anything real via the `a:`
mechanism — `88288338.xyz` has no A record at all.

Retire `default._domainkey.88288338.xyz` once `mail._domainkey` is verified.

## Cutover

Downtime is free here: the old forwarder has handled **zero** legitimate
messages — 0 delivered, 0 queued, 0 bounced in the whole log — so there is
nothing in flight to lose.

1. Publish the DKIM + SPF records and let them propagate.
2. Seal the three secrets above and commit them (do not push yet).
3. Tear the old stack down — **both** commands:

   ```sh
   kubectl -n argocd delete application mailforwarder
   kubectl delete namespace mailforwarder
   ```

   The live Application has **no finalizers**, so deleting it does not cascade:
   the Deployment and Service keep running and nodePort 30025 stays reserved,
   which makes this app's Service fail with `provided port is already
   allocated`. Confirm with `kubectl get ns mailforwarder` → NotFound.
4. Push this repo. This commit adds `mailserver/` and deletes `mailforwarder/`,
   so pushing before step 3 would leave the live Application pointing at a path
   that no longer exists.
5. Apply the haproxy/router/firewall changes for `:587` (ansible repo).
6. Create the app: `ansible-playbook site.yaml --tags role-cluster`, then verify.

The full runbook, including the EdgeOS commands and the Gmail wiring, is in
`ansible-local-network/docs/mailserver.md`.

## Verifying

```sh
# banner + advertised extensions, and confirm AUTH is NOT offered on 25
printf 'EHLO probe\r\nQUIT\r\n' | nc -w 5 192.168.3.40 9025

# AUTH should appear on submission only
printf 'EHLO probe\r\nQUIT\r\n' | nc -w 5 192.168.3.40 9587

# send yourself a message, then watch it leave
kubectl -n mailserver logs -f deploy/mailserver | grep -E 'status=(sent|bounced|deferred)'
```

Send a message to `anything@88288338.xyz` from an outside account. You should
see it delivered to the local mailbox *and* a second `status=sent` for the
SRS-rewritten copy to Gmail.

Then use [mail-tester.com](https://www.mail-tester.com) from the Gmail "send
as" identity to confirm SPF, DKIM and DMARC all pass.

## Phase 2 — real client IPs

Right now every connection arrives SNAT'd from a flannel gateway
(`10.200.{1,2,4}.0`, `10.200.3.1`), so postfix's own abuse controls see "one
client, 1 conn/60s" no matter how hard it is being hit, and fail2ban would be
worse than useless. `externalTrafficPolicy: Local` does **not** fix this —
haproxy re-originates the connection, so it is haproxy's IP either way.

PROXY protocol is the only thing that carries the origin IP through. Flip both
sides in one commit:

- uncomment the two lines in `postfix-master.cf` (configmap.yaml)
- add `send-proxy` to both haproxy backends (ansible repo)
- then set `ENABLE_FAIL2BAN=1` and add `NET_ADMIN` + `NET_RAW` back

Enabling either side alone breaks the listener completely.

## Phase 3 — optional

- `ENABLE_RSPAMD=1` if you start reading over IMAP rather than in Gmail
  (Gmail is doing the filtering today). Costs ~1GB RAM; disable OpenDKIM's
  signer if you let rspamd sign.
- Add `mailserver-data` to the Longhorn S3 backup set. Mail is the kind of
  data whose loss actually hurts — and per the ansible repo's `CLAUDE.md`,
  anything added to the DR bundle has to land in `backup.sh` **and**
  `restore.sh` in the same commit.
