#!/bin/sh
set -eu

umask 077

CONFIG_SOURCE="${GROK2API_CONFIG_SOURCE:-/run/grok2api/config.yaml}"
APP_CONFIG="/app/config.yaml"

log() { echo "[grok2api-entrypoint] $*" >&2; }

yaml_quote() {
  value=$1
  value=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '"%s"' "$value"
}

# Minimal URL-encode for DSN userinfo/path pieces (password-safe).
urlencode() {
  # shellcheck disable=SC2059
  printf '%s' "$1" | awk '
  BEGIN{for(i=0;i<256;i++) ord[sprintf("%c",i)]=i}
  {
    for(i=1;i<=length($0);i++){
      c=substr($0,i,1)
      if (c ~ /[A-Za-z0-9._~-]/) printf "%s", c
      else printf "%%%02X", ord[c]
    }
  }'
}

write_legacy_keys() {
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

resolve_postgres_dsn() {
  # Priority: explicit DSN envs, then Zeabur/compose split vars.
  dsn="${GROK2API_POSTGRES_DSN:-${POSTGRES_DSN:-${DATABASE_URL:-${POSTGRES_CONNECTION_STRING:-}}}}"
  if [ -n "$dsn" ]; then
    # Normalize postgresql:// -> postgres://
    case "$dsn" in
      postgresql://*) dsn="postgres://${dsn#postgresql://}" ;;
    esac
    printf '%s' "$dsn"
    return
  fi

  host="${GROK2API_POSTGRES_HOST:-${POSTGRES_HOST:-${PGHOST:-postgresql.zeabur.internal}}}"
  port="${GROK2API_POSTGRES_PORT:-${POSTGRES_PORT:-${PGPORT:-5432}}}"
  user="${GROK2API_POSTGRES_USER:-${POSTGRES_USER:-${POSTGRES_USERNAME:-${PGUSER:-}}}}"
  pass="${GROK2API_POSTGRES_PASSWORD:-${POSTGRES_PASSWORD:-${PGPASSWORD:-}}}"
  db="${GROK2API_POSTGRES_DB:-${POSTGRES_DB:-${POSTGRES_DATABASE:-${PGDATABASE:-}}}}"
  sslmode="${GROK2API_POSTGRES_SSLMODE:-${POSTGRES_SSLMODE:-disable}}"

  if [ -z "$user" ] || [ -z "$pass" ] || [ -z "$db" ]; then
    return 1
  fi

  user_enc=$(urlencode "$user")
  pass_enc=$(urlencode "$pass")
  db_enc=$(urlencode "$db")
  printf 'postgres://%s:%s@%s:%s/%s?sslmode=%s' "$user_enc" "$pass_enc" "$host" "$port" "$db_enc" "$sslmode"
}

write_database_block() {
  db_driver="${GROK2API_DATABASE_DRIVER:-${DATABASE_DRIVER:-sqlite}}"
  # auto-select postgres if DSN-like envs present and driver not forced
  if [ "$db_driver" = "sqlite" ] || [ -z "$db_driver" ]; then
    if [ -n "${GROK2API_POSTGRES_DSN:-${POSTGRES_DSN:-${DATABASE_URL:-${POSTGRES_CONNECTION_STRING:-}}}}" ] \
      || [ -n "${POSTGRES_HOST:-${PGHOST:-}}" ]; then
      # Only auto-switch if user explicitly asked via GROK2API_DATABASE_DRIVER=postgres OR left default and provided DSN.
      if [ -n "${GROK2API_DATABASE_DRIVER:-}" ]; then
        :
      elif [ -n "${GROK2API_POSTGRES_DSN:-${POSTGRES_DSN:-${DATABASE_URL:-${POSTGRES_CONNECTION_STRING:-}}}}" ]; then
        db_driver="postgres"
      fi
    fi
  fi

  sqlite_path="${GROK2API_SQLITE_PATH:-/app/data/backend.db}"
  max_open="${GROK2API_POSTGRES_MAX_OPEN_CONNS:-50}"
  max_idle="${GROK2API_POSTGRES_MAX_IDLE_CONNS:-10}"

  case "$db_driver" in
    postgres|postgresql|pg)
      if ! postgres_dsn=$(resolve_postgres_dsn); then
        log "ERROR: postgres selected but no DSN/credentials found"
        log "Set GROK2API_POSTGRES_DSN=postgres://user:pass@postgresql.zeabur.internal:5432/db?sslmode=disable"
        log "or POSTGRES_HOST/USER/PASSWORD/DB (Zeabur template vars)"
        exit 1
      fi
      # redact password for logs
      redacted=$(printf '%s' "$postgres_dsn" | sed -E 's#(postgres://[^:]+:)[^@]+@#\1***@#')
      log "database=postgres dsn=$redacted"
      printf 'database:\n'
      printf '  driver: postgres\n'
      printf '  postgres:\n'
      printf '    dsn: %s\n' "$(yaml_quote "$postgres_dsn")"
      printf '    maxOpenConns: %s\n' "$max_open"
      printf '    maxIdleConns: %s\n' "$max_idle"
      ;;
    sqlite|"")
      log "database=sqlite path=$sqlite_path"
      printf 'database:\n'
      printf '  driver: sqlite\n'
      printf '  sqlite:\n'
      printf '    path: %s\n' "$(yaml_quote "$sqlite_path")"
      ;;
    *)
      log "ERROR: unsupported GROK2API_DATABASE_DRIVER=$db_driver (use sqlite or postgres)"
      exit 1
      ;;
  esac
}

generate_config_from_env() {
  jwt_secret="${GROK2API_JWT_SECRET:-${JWT_SECRET:-}}"
  enc_key="${GROK2API_CREDENTIAL_ENCRYPTION_KEY:-${CREDENTIAL_ENCRYPTION_KEY:-}}"
  admin_user="${GROK2API_ADMIN_USERNAME:-${ADMIN_USERNAME:-admin}}"
  admin_pass="${GROK2API_ADMIN_PASSWORD:-${ADMIN_PASSWORD:-}}"
  public_url="${GROK2API_PUBLIC_API_BASE_URL:-${PUBLIC_API_BASE_URL:-}}"
  secure_cookies="${GROK2API_SECURE_COOKIES:-${SECURE_COOKIES:-}}"
  media_path="${GROK2API_MEDIA_PATH:-/app/data/media}"
  static_path="${GROK2API_STATIC_PATH:-/app/frontend/dist}"

  missing=""
  [ -n "$jwt_secret" ] || missing="$missing GROK2API_JWT_SECRET"
  [ -n "$enc_key" ] || missing="$missing GROK2API_CREDENTIAL_ENCRYPTION_KEY"
  [ -n "$admin_pass" ] || missing="$missing GROK2API_ADMIN_PASSWORD"
  [ -n "$public_url" ] || missing="$missing GROK2API_PUBLIC_API_BASE_URL"
  if [ -n "$missing" ]; then
    log "ERROR: missing required env vars:$missing"
    log "Also check Variable names are exact (case-sensitive)."
    exit 1
  fi

  case "$public_url" in
    http://*|https://*|HTTP://*|HTTPS://*) ;;
    *)
      log "ERROR: GROK2API_PUBLIC_API_BASE_URL must start with http:// or https:// (got: $public_url)"
      exit 1
      ;;
  esac

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
  log "publicApiBaseURL=$public_url secureCookies=$secure_cookies"

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
    write_database_block
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

  log "generated config from env -> $APP_CONFIG"
}

log "starting entrypoint"
log "image expects port 8000; set Zeabur container port = 8000"

if [ -f "$CONFIG_SOURCE" ] && [ -s "$CONFIG_SOURCE" ]; then
  cp "$CONFIG_SOURCE" "$APP_CONFIG"
  log "loaded config file $CONFIG_SOURCE"
elif [ -n "${GROK2API_CONFIG_YAML:-}" ]; then
  printf '%s\n' "$GROK2API_CONFIG_YAML" > "$APP_CONFIG"
  log "loaded GROK2API_CONFIG_YAML"
elif [ -n "${GROK2API_CONFIG_B64:-}" ]; then
  printf '%s' "$GROK2API_CONFIG_B64" | base64 -d > "$APP_CONFIG"
  log "loaded GROK2API_CONFIG_B64"
else
  generate_config_from_env
fi

chown grok2api:grok2api "$APP_CONFIG"
chmod 0600 "$APP_CONFIG"
mkdir -p /app/data /app/data/media
chown -R grok2api:grok2api /app/data 2>/dev/null || true

log "exec: $*"
exec su-exec grok2api:grok2api "$@"
