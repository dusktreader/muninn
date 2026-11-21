#!/usr/bin/env bash
set -euo pipefail

namespace="${DEVSPACE_NS:-muninn-local}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl get secret muninn-keycloak-admin --namespace "$namespace" >/dev/null 2>&1; then
  admin_password="$(openssl rand -base64 36 | tr -d '\n')"
  kubectl create secret generic muninn-keycloak-admin \
    --namespace "$namespace" \
    --from-literal=username=muninn-admin \
    --from-literal=password="$admin_password" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  unset admin_password
fi

kubectl apply --namespace "$namespace" -f "$script_dir/keycloak.yaml"
kubectl rollout status statefulset/muninn-keycloak --namespace "$namespace" --timeout=180s

pod="$(kubectl get pods --namespace "$namespace" -l app=muninn-keycloak -o jsonpath='{.items[0].metadata.name}')"
kubectl exec --namespace "$namespace" "$pod" -- bash -lc '
set -euo pipefail
kcadm=/opt/keycloak/bin/kcadm.sh
"$kcadm" config credentials --server http://localhost:8080 --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
client_id=$("$kcadm" get clients -r master -q clientId=muninn-web --fields id --format csv --noquotes | tr -d "\r")
get_mapper_id() {
  local wanted_name="$1"
  local all_mappers mapper_id mapper_name
  all_mappers=$("$kcadm" get clients/"$client_id"/protocol-mappers/models -r master --fields id,name --format csv --noquotes | tr -d "\r")
  while IFS=, read -r mapper_id mapper_name; do
    if [[ "$mapper_name" == "$wanted_name" ]]; then
      printf "%s" "$mapper_id"
      return 0
    fi
  done <<< "$all_mappers"
}
existing_user_id=$("$kcadm" get users -r master -q username=test-user --fields id --format csv --noquotes | tr -d "\r")
capabilities_mapper_id=""
if [[ -n "$client_id" ]]; then
  capabilities_mapper_id=$(get_mapper_id muninn-capabilities)
fi
capabilities_mapper_configured=false
if [[ -n "$client_id" && -n "$capabilities_mapper_id" ]]; then
  existing_capabilities_mapper=$("$kcadm" get clients/"$client_id"/protocol-mappers/models/"$capabilities_mapper_id" -r master --format json)
  capabilities_mapper_configured=true
  for config_entry in "\"protocol\" : \"openid-connect\"" "\"protocolMapper\" : \"oidc-usermodel-attribute-mapper\"" "\"user.attribute\" : \"capabilities\"" "\"claim.name\" : \"capabilities\"" "\"jsonType.label\" : \"String\"" "\"multivalued\" : \"true\"" "\"access.token.claim\" : \"true\"" "\"id.token.claim\" : \"false\"" "\"userinfo.token.claim\" : \"false\""; do
    if [[ "$existing_capabilities_mapper" != *"$config_entry"* ]]; then
      capabilities_mapper_configured=false
      break
    fi
  done
fi
capabilities_attribute_present=false
if [[ -n "$existing_user_id" ]]; then
  existing_user_attributes=$("$kcadm" get users/"$existing_user_id" -r master --format json)
  if [[ "$existing_user_attributes" == *\"capabilities\"* ]]; then
    capabilities_attribute_present=true
  fi
fi
if [[ -n "$client_id" && -n "$existing_user_id" && "$capabilities_mapper_configured" == true && "$capabilities_attribute_present" == true ]]; then
  printf "%s\n" "Keycloak already configured for muninn-web and test-user; skipping setup."
  exit 0
fi
"$kcadm" update users/profile -r master -s unmanagedAttributePolicy=ADMIN_EDIT
if [[ -z "$client_id" ]]; then
  "$kcadm" create clients -r master \
    -s clientId=muninn-web \
    -s enabled=true \
    -s publicClient=true \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s implicitFlowEnabled=false \
    -s serviceAccountsEnabled=false \
    -s '\''attributes."pkce.code.challenge.method"=S256'\'' \
    -s '\''attributes."post.logout.redirect.uris"=http://localhost:4173'\'' \
    -s '\''redirectUris=["http://localhost:4173/callback","http://localhost:4173/"]'\'' \
    -s '\''webOrigins=["http://localhost:4173"]'\''
  client_id=$("$kcadm" get clients -r master -q clientId=muninn-web --fields id --format csv --noquotes | tr -d "\r")
else
  "$kcadm" update clients/"$client_id" -r master \
    -s enabled=true \
    -s publicClient=true \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s implicitFlowEnabled=false \
    -s serviceAccountsEnabled=false \
    -s '\''attributes."pkce.code.challenge.method"=S256'\'' \
    -s '\''attributes."post.logout.redirect.uris"=http://localhost:4173'\'' \
    -s '\''redirectUris=["http://localhost:4173/callback","http://localhost:4173/"]'\'' \
    -s '\''webOrigins=["http://localhost:4173"]'\''
fi

mapper_id=$(get_mapper_id muninn-api-audience)
if [[ -z "$mapper_id" ]]; then
  "$kcadm" create clients/"$client_id"/protocol-mappers/models -r master \
    -s name=muninn-api-audience \
    -s protocol=openid-connect \
    -s protocolMapper=oidc-audience-mapper \
    -s '\''config={"included.custom.audience":"muninn-api","access.token.claim":"true","id.token.claim":"false"}'\''
else
  "$kcadm" update clients/"$client_id"/protocol-mappers/models/"$mapper_id" -r master \
    -s name=muninn-api-audience \
    -s protocol=openid-connect \
    -s protocolMapper=oidc-audience-mapper \
    -s '\''config={"included.custom.audience":"muninn-api","access.token.claim":"true","id.token.claim":"false"}'\''
fi

capabilities_mapper_id=$(get_mapper_id muninn-capabilities)
if [[ -z "$capabilities_mapper_id" ]]; then
  "$kcadm" create clients/"$client_id"/protocol-mappers/models -r master \
    -s name=muninn-capabilities \
    -s protocol=openid-connect \
    -s protocolMapper=oidc-usermodel-attribute-mapper \
    -s '\''config={"user.attribute":"capabilities","claim.name":"capabilities","jsonType.label":"String","multivalued":"true","access.token.claim":"true","id.token.claim":"false","userinfo.token.claim":"false"}'\''
else
  "$kcadm" update clients/"$client_id"/protocol-mappers/models/"$capabilities_mapper_id" -r master \
    -s name=muninn-capabilities \
    -s protocol=openid-connect \
    -s protocolMapper=oidc-usermodel-attribute-mapper \
    -s '\''config={"user.attribute":"capabilities","claim.name":"capabilities","jsonType.label":"String","multivalued":"true","access.token.claim":"true","id.token.claim":"false","userinfo.token.claim":"false"}'\''
fi

user_id=$("$kcadm" get users -r master -q username=test-user --fields id --format csv --noquotes | tr -d "\r")
if [[ -z "$user_id" ]]; then
  "$kcadm" create users -r master -s username=test-user -s enabled=true -s emailVerified=true
  user_id=$("$kcadm" get users -r master -q username=test-user --fields id --format csv --noquotes | tr -d "\r")
else
  "$kcadm" update users/"$user_id" -r master -s enabled=true -s emailVerified=true
fi
"$kcadm" update users/"$user_id" -r master \
  -s '\''attributes.capabilities=["context:read","metadata:write","deletion:write","processing:admin","notes:human:create","notes:human:edit","notes:human:delete"]'\''
updated_user_attributes=$("$kcadm" get users/"$user_id" -r master --format json)
if [[ "$updated_user_attributes" != *'"capabilities"'* ]]; then
  printf "%s\n" "Failed to set test-user capabilities attribute." >&2
  exit 1
fi
"$kcadm" update users/"$user_id"/reset-password -r master -s type=password -s value=test-pass -s temporary=false -n
printf "%s\n" "Configured muninn-web and test-user in realm master."
'
