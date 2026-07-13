#!/bin/sh
set -eu

umask 077

CONFIG_SOURCE="${GROK2API_CONFIG_SOURCE:-/run/grok2api/config.yaml}"
APP_CONFIG="/app/config.yaml"

yaml_quote() {
  # Emit a double-quoted YAML string (escape \ and ").
  value=$1
  value=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '"%s"' "$value"
}

write_legacy_keys() {
  # GROK2API_LEGACY_API_KEYS: comma-separated plain keys
  keys="${GROK2API_LEGACY_API_KEYS:-${LEGACY_API_KEYS:-}}"
  if [ -z "$keys" ]; then
    printf '  legacyAPIKeys: []\n'
    return
  fi
  printf '  legacyAPIKeys:\n'
  old_ifs=$IFS
  IFS=','
  # shellcheck disable=SC2086
  set -- $keys
  IFS=$old_ifs
  for key in "$@"; do
    key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    if [ -n "$key" ]; then
      printf '    - %s\n' "$(yaml_quote "$key")"
    fi
  done
}

generate_config_from_env() {
  jwt_secret="${GROK2API_JWT_SECRET:-${JWT_SECRET:-}}"
  enc_key="${GROK2API_CREDENTIAL_ENCRYPTION_KEY:-${CREDENTIAL_ENCRYPTION_KEY:-}}"
  admin_user="${GROK2API_ADMIN_USERNAME:-${ADMIN_USERNAME:-admin}}"
  admin_pass="${GROK2API_ADMIN_PASSWORD:-${ADMIN_PASSWORD:-}}"
  public_url="${GROK2API_PUBLIC_API_BASE_URL:-${PUBLIC_API_BASE_URL:-}}"
  secure_cookies="${GROK2API_SECURE_COOKIES:-${SECURE_COOKIES:-}}"
  sqlite_path="${GROK2API_SQLITE_PATH:-/app/data/backend.db}"
  media_path="${GROK2API_MEDIA_PATH:-/app/data/media}"
  static_path="${GROK2API_STATIC_PATH:-/app/frontend/dist}"

  missing=""
  [ -n "$jwt_secret" ] || missing="$missing GROK2API_JWT_SECRET"
  [ -n "$enc_key" ] || missing="$missing GROK2API_CREDENTIAL_ENCRYPTION_KEY"
  [ -n "$admin_pass" ] || missing="$missing GROK2API_ADMIN_PASSWORD"
  [ -n "$public_url" ] || missing="$missing GROK2API_PUBLIC_API_BASE_URL"
  if [ -n "$missing" ]; then
    echo "missing config file and required env vars:$missing" >&2
    echo "Either mount config.yaml to ${CONFIG_SOURCE}" >&2
    echo "or set env vars for Zeabur-style deploy (see docs/MIGRATION-GO-CONSOLE.md)." >&2
    exit 1
  fi

  if [ -z "$secure_cookies" ]; then
    case "$public_url" in
      https://*|HTTPS://*) secure_cookies="true" ;;
      *) secure_cookies="false" ;;
    esac
  fi

  case "$secure_cookies" in
    1|true|TRUE|yes|YES|on|ON) secure_cookies="true" ;;
    *) secure_cookies="false" ;;
  esac

  mkdir -p /app/data /app/data/media

  {
    printf 'server:\n'
    printf '  listen: "0.0.0.0:8000"\n'
    printf '  maxBodyBytes: 33554432\n'
    printf '  readTimeout: 15m\n'
    printf '  requestTimeout: 2h\n'
    printf '  swaggerEnabled: false\n'
    printf '\n'
    printf 'secrets:\n'
    printf '  jwtSecret: %s\n' "$(yaml_quote "$jwt_secret")"
    printf '  credentialEncryptionKey: %s\n' "$(yaml_quote "$enc_key")"
    printf '\n'
    printf 'bootstrapAdmin:\n'
    printf '  username: %s\n' "$(yaml_quote "$admin_user")"
    printf '  password: %s\n' "$(yaml_quote "$admin_pass")"
    printf '\n'
    printf 'frontend:\n'
    printf '  publicApiBaseURL: %s\n' "$(yaml_quote "$public_url")"
    printf '  staticPath: %s\n' "$(yaml_quote "$static_path")"
    printf '\n'
    printf 'database:\n'
    printf '  driver: sqlite\n'
    printf '  sqlite:\n'
    printf '    path: %s\n' "$(yaml_quote "$sqlite_path")"
    printf '\n'
    printf 'runtimeStore:\n'
    printf '  driver: memory\n'
    printf '\n'
    printf 'auth:\n'
    printf '  accessTokenTTL: 15m\n'
    printf '  refreshTokenTTL: 720h\n'
    printf '  secureCookies: %s\n' "$secure_cookies"
    printf '\n'
    printf 'media:\n'
    printf '  driver: local\n'
    printf '  local:\n'
    printf '    path: %s\n' "$(yaml_quote "$media_path")"
    printf '\n'
    printf 'compat:\n'
    write_legacy_keys
    printf '\n'
    printf 'provider:\n'
    printf '  build:\n'
    printf '    baseURL: "https://cli-chat-proxy.grok.com/v1"\n'
    printf '    clientVersion: "0.2.99"\n'
    printf '    clientIdentifier: "grok-shell"\n'
    printf '    tokenAuth: "xai-grok-cli"\n'
    printf '    userAgent: "grok-shell/0.2.99 (linux; x86_64)"\n'
    printf '  web:\n'
    printf '    baseURL: "https://grok.com"\n'
    printf '  console:\n'
    printf '    responsesURL: "https://console.x.ai/v1/responses"\n'
    printf '    cluster: "https://us-east-1.api.x.ai"\n'
    printf '    teamId: ""\n'
    printf '    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"\n'
    printf '    enableSearchTools: true\n'
    printf '    timeoutSeconds: 300\n'
    printf '    quotaLimit: 150\n'
    printf '    quotaWindowSeconds: 86400\n'
    printf '    streamHeartbeatInterval: 15\n'
  } > "$APP_CONFIG"

  echo "generated config from environment variables -> ${APP_CONFIG}" >&2
}

if [ -f "$CONFIG_SOURCE" ]; then
  cp "$CONFIG_SOURCE" "$APP_CONFIG"
  echo "loaded config from ${CONFIG_SOURCE}" >&2
elif [ -n "${GROK2API_CONFIG_YAML:-}" ]; then
  printf '%s\n' "$GROK2API_CONFIG_YAML" > "$APP_CONFIG"
  echo "loaded config from GROK2API_CONFIG_YAML env" >&2
elif [ -n "${GROK2API_CONFIG_B64:-}" ]; then
  # Alpine busybox base64 supports -d
  printf '%s' "$GROK2API_CONFIG_B64" | base64 -d > "$APP_CONFIG"
  echo "loaded config from GROK2API_CONFIG_B64 env" >&2
else
  generate_config_from_env
fi

chown grok2api:grok2api "$APP_CONFIG"
chmod 0600 "$APP_CONFIG"

# Ensure data dirs exist and are writable by runtime user.
mkdir -p /app/data /app/data/media
chown -R grok2api:grok2api /app/data 2>/dev/null || true

exec su-exec grok2api:grok2api "$@"