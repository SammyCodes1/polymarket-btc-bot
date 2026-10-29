#!/bin/bash
# polybot watchdog: restart the service if it is ACTIVE but has not written data/bot.log for STALE seconds
# (the bot logs every ~5s loop even when halted). Uses systemctl only; never starts a bot process itself.
# WATCHDOG_DRY_RUN=1 -> report what it would do, restart nothing.
BOT=/home/ubuntu/polymarket-btc-bot
LOG=$BOT/data/bot.log; WLOG=$BOT/data/watchdog.log; STAMP=$BOT/data/.watchdog_last_restart
STALE=${WATCHDOG_STALE_SECONDS:-300}; COOL=${WATCHDOG_COOLDOWN_SECONDS:-900}
now=$(date +%s)
say() { echo "$(date -u '+%F %T') watchdog: $*" | tee -a "$WLOG"; }
if [ "$(systemctl is-active polybot)" != "active" ]; then
  [ -n "$WATCHDOG_DRY_RUN" ] && say "[dry] polybot not active: nothing to do (stopped on purpose or systemd is restarting it)"
  exit 0
fi
since=$(date -d "$(systemctl show -p ActiveEnterTimestamp --value polybot)" +%s 2>/dev/null || echo 0)
up=$((now - since))
if [ ! -f "$LOG" ]; then say "bot.log missing"; age=999999; else age=$((now - $(stat -c %Y "$LOG"))); fi
if [ "$age" -le "$STALE" ]; then
  [ -n "$WATCHDOG_DRY_RUN" ] && say "[dry] ok: log age ${age}s <= ${STALE}s (service up ${up}s)"
  exit 0
fi
if [ "$up" -le "$STALE" ]; then say "log stale ${age}s but service only up ${up}s: waiting"; exit 0; fi
last=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$COOL" ]; then say "log stale ${age}s but last restart was $((now - last))s ago (< ${COOL}s): skipping"; exit 0; fi
if [ -n "$WATCHDOG_DRY_RUN" ]; then say "[dry] WOULD restart polybot: log stale ${age}s (> ${STALE}s)"; exit 0; fi
say "log stale ${age}s (> ${STALE}s) while polybot active: restarting"
echo "$now" > "$STAMP"
sudo -n /bin/systemctl restart polybot && say "restart issued" || say "restart FAILED"
