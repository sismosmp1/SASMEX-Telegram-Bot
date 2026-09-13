# Bot SASMEX -> Telegram

Este proyecto revisa `https://rss.sasmex.net/` cada 15 segundos, detecta un CAP nuevo y lo publica en un canal de Telegram.

## Variables
- `BOT_TOKEN`: token de @SismosMP_bot (NO lo publiques)
- `CHAT_ID`: identificador del canal. Para un canal público puede ser `@NombreDelCanal`; para el canal privado de pruebas necesitaremos obtener el chat_id numérico.
- `CHECK_SECONDS`: 15
- `SASMEX_URL`: https://rss.sasmex.net/

## Importante
La primera vez que arranca, el bot memoriza el CAP más reciente y NO lo publica, para evitar mandar un sismo histórico. Solo publicará los siguientes CAP nuevos.

## Despliegue
Pensado para Render como Web Service. Render Free tiene 750 horas/mes y los servicios gratuitos se duermen tras 15 min sin tráfico; para mantenerlo despierto se puede usar un monitor externo que visite `/health` cada pocos minutos. Ver documentación oficial de Render.
