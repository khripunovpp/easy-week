#!/usr/bin/env bash
# Установка мониторинг-стека на Raspberry Pi (native, без Docker):
# Prometheus + Loki + Promtail (бинарники + systemd) и Grafana (apt).
# Идемпотентно: можно перезапускать. Требует sudo (у pashtitto без пароля).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MON="$REPO_DIR/monitoring"

PROM_VER="2.53.2"
LOKI_VER="3.3.2"

echo "→ версии: Prometheus $PROM_VER, Loki/Promtail $LOKI_VER, Grafana (apt latest)"

# --- 1. Бинарники Prometheus / Loki / Promtail → /usr/local/bin ---
tmp="$(mktemp -d)"
cd "$tmp"

echo "→ Prometheus"
curl -fsSL -o prom.tar.gz \
  "https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/prometheus-${PROM_VER}.linux-arm64.tar.gz"
tar xzf prom.tar.gz
sudo install -m 0755 "prometheus-${PROM_VER}.linux-arm64/prometheus" /usr/local/bin/prometheus
sudo install -m 0755 "prometheus-${PROM_VER}.linux-arm64/promtool" /usr/local/bin/promtool

echo "→ Loki + Promtail"
curl -fsSL -o loki.zip "https://github.com/grafana/loki/releases/download/v${LOKI_VER}/loki-linux-arm64.zip"
curl -fsSL -o promtail.zip "https://github.com/grafana/loki/releases/download/v${LOKI_VER}/promtail-linux-arm64.zip"
unzip -oq loki.zip && unzip -oq promtail.zip
sudo install -m 0755 loki-linux-arm64 /usr/local/bin/loki
sudo install -m 0755 promtail-linux-arm64 /usr/local/bin/promtail

cd / && rm -rf "$tmp"

# --- 2. Каталоги данных (владелец pashtitto) + доступ к journald ---
echo "→ каталоги данных"
sudo mkdir -p /var/lib/prometheus /var/lib/loki /var/lib/promtail
sudo chown -R pashtitto:pashtitto /var/lib/prometheus /var/lib/loki /var/lib/promtail
sudo usermod -aG systemd-journal pashtitto || true

# --- 3. Grafana из официального apt-репозитория ---
if ! command -v grafana-server >/dev/null 2>&1; then
  echo "→ Grafana (apt)"
  sudo apt-get install -y apt-transport-https software-properties-common wget gpg >/dev/null
  sudo mkdir -p /etc/apt/keyrings
  wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg >/dev/null
  echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y grafana >/dev/null
fi

# --- 4. Провижининг Grafana (datasources + дашборд) ---
echo "→ провижининг Grafana"
sudo mkdir -p /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards /var/lib/grafana/dashboards
sudo cp "$MON/grafana/datasources.pi.yml" /etc/grafana/provisioning/datasources/ew.yml
sudo cp "$MON/grafana/dashboards-provider.yml" /etc/grafana/provisioning/dashboards/ew.yml
sudo cp "$MON/grafana/dashboards/easy-week.json" /var/lib/grafana/dashboards/easy-week.json
sudo chown -R grafana:grafana /var/lib/grafana/dashboards

# --- 5. systemd-юниты Prometheus/Loki/Promtail ---
echo "→ systemd-юниты"
sudo cp "$MON/systemd/ew-prometheus.service" "$MON/systemd/ew-loki.service" "$MON/systemd/ew-promtail.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ew-prometheus ew-loki ew-promtail
sudo systemctl enable --now grafana-server
sudo systemctl restart grafana-server

sleep 3
echo
echo "✅ Готово. Статусы:"
systemctl is-active ew-prometheus ew-loki ew-promtail grafana-server | paste -d' ' <(echo -e "prometheus\nloki\npromtail\ngrafana") -
IP="$(hostname -I | awk '{print $1}')"
echo
echo "Grafana:    http://$IP:3000  (admin / admin — сменить при входе)"
echo "Prometheus: http://127.0.0.1:9090 (только локально; метрики LAN — http://$IP:8080/metrics)"
