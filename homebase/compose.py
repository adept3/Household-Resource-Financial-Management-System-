"""Optional Docker Compose file for later-phase services.

The ledger does not need Docker. Every service is in the ``later`` profile,
so ``docker compose up`` starts nothing. Image tags are pinned. Passwords are
environment references with no default. The Docker socket is read-only, and
no service sets privileged mode.
"""

from __future__ import annotations


def generate_docker_compose() -> str:
    return """# HomeBase optional containers. The ledger is Python + SQLite and does not need Docker.
# Every service is in the "later" profile and stays off unless you opt in:
#   docker compose --profile later up -d
# Image tags are pinned. Passwords come from a mode-0600 .env file, never from this file.
# The Docker socket is mounted read-only. No service runs privileged.
# Immich, Frigate, and Paperless are heavy for an 8 GB machine. Read upstream docs
# before enabling them. This file is a starting point, not a supported deployment.

services:
  traefik:
    profiles: ["later"]
    image: traefik:v3.7.19
    container_name: traefik
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik:/etc/traefik
    networks: [proxy]
    mem_limit: 128m

  pihole:
    profiles: ["later"]
    image: pihole/pihole:2026.11.0
    container_name: pihole
    restart: unless-stopped
    ports: ["53:53/tcp", "53:53/udp", "8053:80"]
    environment:
      TZ: ${TZ:-UTC}
      FTLCONF_webserver_api_password: ${PIHOLE_WEBPASSWORD:?Set PIHOLE_WEBPASSWORD in .env}
    volumes:
      - ./pihole/etc-pihole:/etc/pihole
      - ./pihole/etc-dnsmasq.d:/etc/dnsmasq.d
    cap_add: [NET_ADMIN]
    networks: [proxy]
    mem_limit: 256m

  unbound:
    profiles: ["later"]
    image: mvance/unbound:1.22.0
    container_name: unbound
    restart: unless-stopped
    volumes: [./unbound:/opt/unbound/etc/unbound]
    networks: [proxy]
    mem_limit: 128m

  wireguard:
    profiles: ["later"]
    image: linuxserver/wireguard:1.0.20260223
    container_name: wireguard
    cap_add: [NET_ADMIN, SYS_MODULE]
    ports: ["51820:51820/udp"]
    environment:
      PEERS: ${WIREGUARD_PEERS:?Set WIREGUARD_PEERS in .env}
      PEERDNS: ${WIREGUARD_PEER_DNS:?Set WIREGUARD_PEER_DNS in .env}
      SERVERURL: ${WIREGUARD_SERVER_URL:?Set WIREGUARD_SERVER_URL in .env}
    volumes: [./wireguard:/config, /lib/modules:/lib/modules:ro]
    restart: unless-stopped
    mem_limit: 64m

  authelia:
    profiles: ["later"]
    image: authelia/authelia:4.39.28
    container_name: authelia
    volumes: [./authelia:/config]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  crowdsec:
    profiles: ["later"]
    image: crowdsecurity/crowdsec:v1.8.2
    container_name: crowdsec
    volumes: [./crowdsec:/etc/crowdsec, /var/log:/var/log:ro]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  vaultwarden:
    profiles: ["later"]
    image: vaultwarden/server:1.37.5
    container_name: vaultwarden
    volumes: [./vaultwarden:/data]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  actual-budget:
    profiles: ["later"]
    image: actualbudget/actual-server:25.11.0
    container_name: actual-budget
    volumes: [./actual-budget:/data]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  grocy:
    profiles: ["later"]
    image: linuxserver/grocy:4.7.1
    container_name: grocy
    volumes: [./grocy:/config]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  mealie:
    profiles: ["later"]
    image: ghcr.io/mealie-recipes/mealie:v3.31.0
    container_name: mealie
    volumes: [./mealie:/app/data]
    environment:
      ALLOW_SIGNUP: "false"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  paperless-ngx:
    profiles: ["later"]
    image: ghcr.io/paperless-ngx/paperless-ngx:v3.4.2
    container_name: paperless
    volumes:
      - ./paperless/data:/usr/src/paperless/data
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 1g

  immich-server:
    profiles: ["later"]
    image: ghcr.io/immich-app/immich-server:v3.4.1
    container_name: immich-server
    volumes: [./immich/upload:/usr/src/app/upload]
    environment:
      DB_HOSTNAME: immich-db
      DB_USERNAME: ${IMMICH_DB_USER:-immich}
      DB_PASSWORD: ${IMMICH_DB_PASSWORD:?Set IMMICH_DB_PASSWORD in .env}
      DB_DATABASE_NAME: ${IMMICH_DB_NAME:-immich}
      REDIS_HOSTNAME: immich-redis
    depends_on: [immich-db, immich-redis]
    networks: [proxy, immich]
    restart: unless-stopped
    mem_limit: 1g

  immich-ml:
    profiles: ["later"]
    image: ghcr.io/immich-app/immich-machine-learning:v3.4.1
    container_name: immich-ml
    volumes: [./immich/model-cache:/cache]
    networks: [immich]
    restart: unless-stopped
    mem_limit: 2g

  immich-db:
    profiles: ["later"]
    image: tensorchord/pgvecto-rs:pg16-v0.3.0
    container_name: immich-db
    volumes: [./immich/db:/var/lib/postgresql/data]
    environment:
      POSTGRES_PASSWORD: ${IMMICH_DB_PASSWORD:?Set IMMICH_DB_PASSWORD in .env}
      POSTGRES_USER: ${IMMICH_DB_USER:-immich}
      POSTGRES_DB: ${IMMICH_DB_NAME:-immich}
    networks: [immich]
    restart: unless-stopped
    mem_limit: 512m

  immich-redis:
    profiles: ["later"]
    image: redis:8.2.7-alpine
    container_name: immich-redis
    networks: [immich]
    restart: unless-stopped
    mem_limit: 64m

  frigate:
    profiles: ["later"]
    image: ghcr.io/blakeblackshear/frigate:0.18.2
    container_name: frigate
    volumes:
      - ./frigate/config.yml:/config/config.yml:ro
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 1g

  n8n:
    profiles: ["later"]
    image: n8nio/n8n:2.47.7
    container_name: n8n
    volumes: [./n8n:/home/node/.n8n]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  ntfy:
    profiles: ["later"]
    image: binwiederhier/ntfy:v2.38.0
    container_name: ntfy
    command: serve
    volumes: [./ntfy:/var/lib/ntfy]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 64m

  grafana:
    profiles: ["later"]
    image: grafana/grafana-oss:13.0.2
    container_name: grafana
    volumes: [./grafana:/var/lib/grafana]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  uptime-kuma:
    profiles: ["later"]
    image: louislam/uptime-kuma:2.5.11
    container_name: uptime-kuma
    volumes: [./uptime-kuma:/app/data]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  homepage:
    profiles: ["later"]
    image: gethomepage/homepage:v2.8.0
    container_name: homepage
    volumes: [./homepage:/app/config]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  syncthing:
    profiles: ["later"]
    image: syncthing/syncthing:2.1.7
    container_name: syncthing
    volumes: [./syncthing:/var/syncthing]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  stirling-pdf:
    profiles: ["later"]
    image: stirlingtools/stirling-pdf:3.3.0
    container_name: stirling-pdf
    networks: [proxy]
    restart: "no"
    mem_limit: 512m

  portainer:
    profiles: ["later"]
    image: portainer/portainer-ce:2.45.3
    container_name: portainer
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./portainer:/data
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  dozzle:
    profiles: ["later"]
    image: amir20/dozzle:v11.4.3
    container_name: dozzle
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 64m

networks:
  proxy:
    driver: bridge
  immich:
    driver: bridge
"""
