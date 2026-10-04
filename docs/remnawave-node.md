# Remnawave: панель и нода на одном сервере

Инструкция использует условные имена `panel.example.net`, `sub.example.net`, `hub.example.net` и `shop.example.net`. Замените их своими значениями. `127.0.0.1` — стандартный локальный адрес; внутренние порты не публикуются.

## Схема подключения

```text
Клиент → nginx stream :443, выбор по TLS SNI
  shop.example.net → 127.0.0.1:10443, Xray REALITY
                         └→ target 127.0.0.1:9443, nginx HTTPS
  остальные имена  → 127.0.0.1:9443, nginx HTTPS
```

Панель, страница подписки и остальные HTTPS-сервисы работают через nginx независимо от процесса ноды. Административный API ноды использует отдельный внутренний порт 2222.

## Подготовка

Исходник статической страницы находится в `etc/nginx/static/shop/index.html`, логотип — в `webapp/static/img/hamzavpn-logo.png`. Перед размещением задайте ссылки своего проекта, скопируйте страницу и логотип в каталог сайта и дайте nginx права чтения. После согласования кассы уберите временную проверочную метку также со статической страницы.

1. Создайте DNS-записи для нужных имён. Для имени подключения ноды используйте режим DNS only.
2. Выпустите действительные TLS-сертификаты. Сайт `shop.example.net` должен открываться напрямую, поддерживать TLS 1.3 и HTTP/2. Размещайте на нём собственные материалы проекта.
3. Сохраните резервную копию базы панели, её настроек, nginx-конфигурации и задания обновления сертификатов.
4. Убедитесь, что nginx имеет stream и ssl_preread. Закрепите проверенную версию образа ноды, не используя изменяемый тег `latest`.

## nginx и PROXY protocol

Перенесите все HTTPS-vhosts с публичного443 на `127.0.0.1:9443`. Во всех этих vhosts используйте:

```nginx
listen 127.0.0.1:9443 ssl proxy_protocol;
set_real_ip_from 127.0.0.1;
real_ip_header proxy_protocol;
port_in_redirect off;
```

Включите HTTP/2 синтаксисом, подходящим установленной версии nginx. Укажите соответствующий сертификат и `server_name` для каждого vhost. Передавайте приложениям правильный протокол HTTPS и адрес клиента.

В nginx stream создайте маршрут:

```nginx
upstream service_web {
    server 127.0.0.1:9443;
}
upstream service_node {
    server 127.0.0.1:10443 max_fails=1 fail_timeout=3s;
    server 127.0.0.1:9443 backup;
}
map $ssl_preread_server_name $service_upstream {
    shop.example.net service_node;
    default service_web;
}
server {
    listen 443;
    listen [::]:443;
    ssl_preread on;
    proxy_protocol on;
    proxy_connect_timeout 3s;
    proxy_timeout 1h;
    proxy_next_upstream on;
    proxy_pass $service_upstream;
}
```

На публичном `listen443` не добавляйте `proxy_protocol`: клиент передаёт обычный TLS. nginx создаёт заголовок для внутреннего соединения. Внутренний HTTPS-listener принимает его перед TLS.

## Нода и профиль

Разместите compose и `.env` в отдельном каталоге с правами700; `.env` должен иметь права600. Получите `SECRET_KEY` штатным способом из своей панели и сохраните только в закрытом `.env`.

```yaml
services:
  remnanode:
    image: remnawave/node:<verified-version>
    restart: unless-stopped
    network_mode: host
    cap_add: [NET_ADMIN]
    env_file: .env
    mem_limit: 512m
    cpus: 1.0
    pids_limit: 256
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: 3
```

Используйте `NODE_PORT=2222`. Параметры capabilities и ulimits сверяйте с официальным compose выбранной версии.

Создайте профиль в панели со следующими параметрами VLESS inbound:

- `listen`: `127.0.0.1`, `port`: 10443; уникальный `tag`.
- `streamSettings.network`: `tcp`, `security`: `reality`.
- `tcpSettings.acceptProxyProtocol`: `true`.
- `realitySettings.target`: `127.0.0.1:9443`, `xver`: 2.
- `serverNames`: только `shop.example.net`; новый закрытый privateKey и новый shortId.

Не направляйте target на публичный `shop.example.net:443`: это создаёт повторный проход через тот же маршрут. PROXY protocol должен быть согласован на всех трёх участках: stream → Xray, stream → HTTPS и REALITY → HTTPS.

В панели создайте ноду с адресом хоста, доступным из контейнера панели, и API-портом2222. При Docker bridge адрес `127.0.0.1` внутри контейнера относится к самому контейнеру; используйте шлюз его сети. Назначьте профиль и его inbound ноде.

После подключения ноды создайте host для клиентов: адрес `shop.example.net`, публичный порт 443, соответствующие inbound и SNI. Добавьте inbound в существующий внутренний squad, сохранив прежние назначения пользователей и остальные inbounds.

## Ограничение доступа

До запуска ноды ограничьте TCP2222 в INPUT firewall: разрешите localhost и доверенный источник панели в Docker-сети, остальные входящие подключения отклоняйте. Примените правила для IPv4 и IPv6 и обеспечьте их восстановление при загрузке системы.

При host networking правило только в DOCKER-USER не защищает listener хоста. Проверяйте актуальность адреса контейнера и интерфейса после изменения Docker-сети. Порты10443 и 9443 должны слушать только localhost.

## Проверка и обслуживание

1. Проверьте конфигурацию nginx до reload. На первом этапе можно направить все SNI на HTTPS 9443; маршрут к 10443 включайте после успешного запуска ноды.
2. Убедитесь, что панель показывает подключение ноды, действующие версии и время работы Xray. В подписке должны присутствовать настоящий host443 и правильный SNI.
3. Проверьте подключение временного тестового пользователя к собственному HTTPS-ресурсу. После проверки удалите пользователя и отзовите временный административный API token.
4. Остановите ноду и проверьте доступность панели, страницы подписки и сайта. Резервный upstream сохраняет обычный HTTPS сайта; VPN-соединения требуют работающей ноды. Запустите ноду и повторите проверку.
5. Убедитесь извне, что 2222,10443 и 9443 недоступны.
6. Обновите все скрипты и таймеры, которые генерируют nginx-vhosts: они должны сохранять внутренний 9443 и PROXY protocol. При ошибке этапа восстановления возвращайте прежние файлы и возобновляйте таймер. Проверьте принудительный запуск TLS-задания и обновление сертификата.
7. Перед обновлением снимайте резервную копию и фиксируйте версию образа. Меняйте один компонент за раз, проверяйте HTTPS и реальное клиентское соединение. При сбое восстанавливайте прежнюю версию и конфигурацию.

Храните резервные копии базы панели, закрытых `.env`, ключей профиля, сертификатов, compose, nginx и системных заданий в защищённом хранилище. Проверяйте восстановление. Контролируйте память, CPU, свободное место и размер журналов; при нагрузке корректируйте лимиты 512 MiB и 1 CPU. Общий сервер остаётся общей точкой отказа оборудования.

## Официальные материалы

- [Установка Remnawave Node](https://docs.rw/install/remnawave-node/)
- [Официальные релизы ноды](https://github.com/remnawave/node/releases)
- [nginx: ssl_preread и выбор по SNI](https://nginx.org/en/docs/stream/ngx_stream_ssl_preread_module.html)
- [nginx stream: proxy_protocol и резервные upstream](https://nginx.org/en/docs/stream/ngx_stream_proxy_module.html)
- [nginx: обработка адреса клиента](https://nginx.org/en/docs/http/ngx_http_realip_module.html)
- [Xray: RAW/TCP и acceptProxyProtocol](https://xtls.github.io/en/config/transports/raw.html)
- [Xray: параметры REALITY](https://xtls.github.io/en/config/transports/reality.html)
