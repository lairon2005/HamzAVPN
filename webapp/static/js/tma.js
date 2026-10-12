// webapp/static/js/tma.js
// Telegram Mini App: адаптация webapp/static/js/scripts.js под Telegram.WebApp
// (docs/tma-roadmap.md фаза 2). Не подключается на обычном сайте — только в tma/base.html
// и tma/_auth_splash.html.

(function () {
    'use strict';

    var tg = window.Telegram && window.Telegram.WebApp;
    var TOKEN_KEY = 'HamzAVPN_tma_token';
    var SCREEN_PARAMS = ['tariffs', 'import', 'support', 'history'];

    if (tg) {
        tg.ready();
        tg.expand();
        if (typeof tg.isVersionAtLeast === 'function') {
            if (tg.isVersionAtLeast('6.9')) tg.setHeaderColor('#081222');
            if (tg.isVersionAtLeast('6.1')) tg.setBackgroundColor('#081222');
        }
        if (tg.MainButton && typeof tg.MainButton.setParams === 'function') {
            tg.MainButton.setParams({ color: '#163b75', text_color: '#ffffff' });
        }
        // Тёмная тема сайта — не завязываемся на themeParams клиента (см. roadmap фаза 2).
    }

    // ============================================================
    // Token storage (фолбэк на Authorization, если cookie порезаны в webview)
    // ============================================================

    function getToken() {
        try {
            return sessionStorage.getItem(TOKEN_KEY);
        } catch (e) {
            return null;
        }
    }

    function setToken(token) {
        if (!token) return;
        try {
            sessionStorage.setItem(TOKEN_KEY, token);
        } catch (e) { /* ignore (private mode и т.п.) */ }
    }

    function authHeaders() {
        var token = getToken();
        return token ? { 'Authorization': 'Bearer ' + token } : {};
    }

    // Обёртка над fetch: всегда подставляет Authorization-фолбэк и cookie текущего origin.
    function tmaFetch(url, options) {
        options = options || {};
        var headers = {};
        for (var k in options.headers) headers[k] = options.headers[k];
        var auth = authHeaders();
        for (var k2 in auth) headers[k2] = auth[k2];
        options.headers = headers;
        options.credentials = 'same-origin';
        return fetch(url, options);
    }
    window.tmaFetch = tmaFetch;

    function haptic(style) {
        if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.impactOccurred(style || 'light');
        }
    }

    // ============================================================
    // Toast (свой, без Telegram.WebApp.showAlert — тот модальный и мешает быстрым действиям)
    // ============================================================

    function showToast(message, type) {
        var container = document.getElementById('toast-container');
        if (!container) return;
        var toast = document.createElement('div');
        toast.className = 'toast-item ' + (type || 'info');
        toast.innerHTML = '<span>' + message + '</span>';
        container.appendChild(toast);
        setTimeout(function () {
            toast.classList.add('fade-out');
            setTimeout(function () { toast.remove(); }, 300);
        }, 3000);
    }
    window.tmaToast = showToast;

    // ============================================================
    // Auth splash: initData → POST /tma/auth → sessionStorage → reload
    // ============================================================

    function initAuthSplash() {
        var statusEl = document.getElementById('tmaSplashStatus');
        if (!statusEl) return; // мы не на экране авторизации

        var spinnerEl = document.getElementById('tmaSplashSpinner');
        var titleEl = document.getElementById('tmaSplashTitle');

        function showError(message) {
            if (spinnerEl) spinnerEl.style.display = 'none';
            statusEl.textContent = message;
            statusEl.classList.add('text-danger-custom');
            statusEl.classList.remove('text-gray');
        }

        var initData = tg && tg.initData;
        if (!initData) {
            showError('Откройте это приложение через кнопку в Telegram-боте.');
            return;
        }

        fetch('/tma/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ init_data: initData }),
        })
            .then(function (res) {
                return res.json().then(function (data) { return { ok: res.ok, data: data }; });
            })
            .then(function (result) {
                if (!result.ok || !result.data || !result.data.ok) {
                    var detail = result.data && result.data.detail;
                    showError(detail || 'Ошибка авторизации');
                    return;
                }
                setToken(result.data.token);
                if (spinnerEl) spinnerEl.style.display = 'none';
                statusEl.textContent = 'Готово!';
                statusEl.classList.add('text-success-custom');
                // Перезагружаем ТУ ЖЕ страницу — теперь cookie/сессия валидны,
                // роут отдаст настоящий экран вместо сплэша.
                location.reload();
            })
            .catch(function () {
                showError('Не удалось связаться с сервером');
            });
    }

    // ============================================================
    // Deeplink-роутинг: ?startapp=tariffs|import|support|history → экран.
    // Telegram всегда открывает базовый URL Mini App (/tma/), поэтому редирект
    // делаем только с главной. Числовой start_param — реферальный ID, его
    // обрабатывает /tma/auth на бэкенде, здесь игнорируем.
    // ============================================================

    function routeStartParam() {
        if (!tg) return;
        var raw = tg.initDataUnsafe && tg.initDataUnsafe.start_param;
        if (!raw) return;
        if (location.pathname !== '/tma/') return;
        // Возврат из Platega (return_url = t.me/<bot>/<app>?startapp=paid, см.
        // webapp/routers/payment.py, docs/tma-roadmap.md фаза 3.1) — платёж
        // подтвердит вебхук асинхронно, здесь просто остаёмся на главной и
        // сообщаем пользователю, что оплата обрабатывается.
        if (raw === 'paid') {
            showToast('Оплата обрабатывается…', 'info');
            return;
        }
        if (SCREEN_PARAMS.indexOf(raw) === -1) return;
        location.href = '/tma/' + raw;
    }

    // ============================================================
    // BackButton: показываем на всех экранах кроме главной, ведёт на /tma/
    // ============================================================

    function initBackButton() {
        if (!tg || !tg.BackButton) return;
        var isRoot = document.body && document.body.dataset.tmaRoot === 'true';
        if (isRoot) {
            tg.BackButton.hide();
        } else {
            tg.BackButton.onClick(function () { location.href = '/tma/'; });
            tg.BackButton.show();
        }
    }

    // MainButton может остаться показанным с предыдущего экрана (полная навигация —
    // новый document, но у части клиентов состояние WebApp переживает переход).
    // Сбрасываем по умолчанию, экран тарифов настраивает его сам.
    function resetMainButton() {
        if (!tg || !tg.MainButton) return;
        tg.MainButton.offClick(mainButtonHandler);
        tg.MainButton.hide();
    }

    var mainButtonHandler = function () {};

    // ============================================================
    // "Ещё" — нижняя шторка
    // ============================================================

    function initMoreSheet() {
        var btn = document.getElementById('tmaMoreBtn');
        var sheet = document.getElementById('tmaSheet');
        var backdrop = document.getElementById('tmaSheetBackdrop');
        if (!btn || !sheet || !backdrop) return;

        function open() {
            sheet.classList.add('open');
            backdrop.classList.add('open');
            haptic('light');
        }
        function close() {
            sheet.classList.remove('open');
            backdrop.classList.remove('open');
        }

        btn.addEventListener('click', open);
        backdrop.addEventListener('click', close);
    }

    // ============================================================
    // Copy (VPN-ключ, реферальная ссылка) с haptic feedback
    // ============================================================

    function tmaCopy(inputId, message) {
        var input = document.getElementById(inputId);
        if (!input || !input.value) return;
        navigator.clipboard.writeText(input.value).then(function () {
            haptic('light');
            showToast(message || 'Скопировано!', 'success');
        }).catch(function () {
            showToast('Не удалось скопировать', 'error');
        });
    }
    window.tmaCopy = tmaCopy;

    // ============================================================
    // Оплата: POST /payment/create + Telegram.WebApp.openLink (Фаза 3.1 из roadmap
    // делает это глубже — здесь только базовый флоу, как у initPayment на сайте)
    // ============================================================

    // Локальное состояние pending препятствует одновременному созданию двух счетов.
    // Перед сменой способа оплаты пользователь подтверждает завершение старого заказа.
    function askConfirm(message, onYes) {
        if (tg && tg.showConfirm) {
            tg.showConfirm(message, function (ok) { if (ok) onYes(); });
            return;
        }
        if (confirm(message)) onYes();
    }

    function offerCancelPending(retry) {
        askConfirm(
            'У вас уже есть неоплаченный счёт. Отменить его и выставить новый? ' +
            'Старую ссылку на оплату после этого открывать не нужно.',
            function () {
                tmaFetch('/payment/cancel-pending', { method: 'POST' })
                    .then(function (response) {
                        if (response.ok) {
                            showToast('Счёт отменён, выставляю новый…', 'info');
                            retry();
                        } else if (response.status === 401) {
                            showToast('Сессия истекла, перезагрузите Mini App.', 'error');
                        } else {
                            showToast('Не удалось отменить счёт. Перезагрузите Mini App и попробуйте снова.', 'error');
                        }
                    })
                    .catch(function () {
                        showToast('Ошибка соединения с сервером.', 'error');
                    });
            }
        );
    }

    function formatRubAmount(value) { return Number(value).toFixed(2); }

    async function tmaPaymentQuote(payload) {
        var response = await tmaFetch('/payment/quote', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        var data = await response.json().catch(function () { return {}; });
        if (!response.ok) {
            var error = new Error(data.detail || 'Не удалось рассчитать итоговую стоимость.');
            error.status = response.status;
            throw error;
        }
        return data;
    }

    function confirmPaymentAmount(amount) {
        var message = 'Итог к оплате: ' + formatRubAmount(amount) + ' ₽. Открыть платёжную страницу?';
        return new Promise(function (resolve) {
            if (tg && tg.showConfirm) tg.showConfirm(message, resolve);
            else resolve(confirm(message));
        });
    }

    function confirmAsync(message) {
        return new Promise(function (resolve) {
            if (tg && tg.showConfirm) tg.showConfirm(message, resolve);
            else resolve(confirm(message));
        });
    }

    async function tmaInitPayment(tariffName, price, btn, activePromo, extraDevices, allowCancelRetry, tariffDays) {
        if (!btn) return;
        var toggle = document.getElementById('autorenewToggle');
        var wantAutorenew = !!(toggle && toggle.checked);
        if (wantAutorenew && activePromo) {
            showToast('Автопродление не сочетается с промокодом: уберите промокод или снимите галочку.', 'error');
            return;
        }
        btn.classList.add('btn-loading');
        if (tg && tg.MainButton) tg.MainButton.showProgress();
        var payload = { tariff_name: tariffName, price: price, source: 'tma', extra_devices: extraDevices || 0 };
        if (activePromo) payload.promo_code = activePromo.code;
        if (wantAutorenew) payload.autorenew = true;
        try {
            var quote = await tmaPaymentQuote(payload);
            if (wantAutorenew) {
                if (!quote.autorenew_amount) {
                    showToast('Для этого тарифа автопродление недоступно. Снимите галочку и оплатите разово.', 'error');
                    return;
                }
                if (!await confirmAsync('Подключить автопродление? Каждые ' + (tariffDays || '') + ' дн. через СБП будет списываться ' +
                    quote.autorenew_amount + ' ₽, пока вы не отключите его. Отключить можно в любой момент.')) return;
            }
            if (btn.dataset.quotedAmount && formatRubAmount(btn.dataset.quotedAmount) !== formatRubAmount(quote.final_amount)) {
                if (!await confirmPaymentAmount(quote.final_amount)) return;
            }
            var response = await tmaFetch('/payment/create', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
            });
            var data = await response.json().catch(function () { return {}; });
            if (response.ok) {
                if (data.final_amount != null && formatRubAmount(data.final_amount) !== formatRubAmount(quote.final_amount)) {
                    if (!await confirmPaymentAmount(data.final_amount)) return;
                }
                if (tg && tg.openLink) tg.openLink(data.payment_url);
                else window.open(data.payment_url, '_blank');
                showToast('К оплате ' + formatRubAmount(data.final_amount == null ? quote.final_amount : data.final_amount) + ' ₽. Вернитесь сюда после оплаты.', 'info');
            } else if (response.status === 401) {
                showToast('Сессия истекла, перезагрузите Mini App.', 'error');
            } else if (response.status === 409 && allowCancelRetry !== false) {
                offerCancelPending(function () { tmaInitPayment(tariffName, price, btn, activePromo, extraDevices, false, tariffDays); });
            } else {
                showToast(data.detail || 'Не удалось создать платёж. Попробуйте позже.', 'error');
            }
        } catch (error) {
            showToast(error.status === 401 ? 'Сессия истекла, перезагрузите Mini App.' : error.message || 'Ошибка соединения с сервером.', 'error');
        } finally {
            btn.classList.remove('btn-loading');
            if (tg && tg.MainButton) tg.MainButton.hideProgress();
        }
    }
    window.tmaInitPayment = tmaInitPayment;

    // ============================================================
    // Промокод (тарифы) — как applyPromo() на сайте, но через tmaFetch
    // ============================================================

    var tmaActivePromo = null;
    window.tmaGetActivePromo = function () { return tmaActivePromo; };

    function tmaApplyPromo(onDiscount) {
        var input = document.getElementById('promoInput');
        var resultDiv = document.getElementById('promoResult');
        if (!input || !resultDiv) return;
        var code = input.value.trim();

        if (!code) {
            tmaActivePromo = null;
            resultDiv.textContent = 'Промокод отменён.';
            resetTariffPrices();
            return;
        }
        resultDiv.innerHTML = '<span class="text-gray">Проверяю...</span>';

        tmaFetch('/payment/validate-promo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.valid) {
                    if (data.type === 'discount') {
                        tmaActivePromo = { code: data.code, discount_percent: data.discount_percent };
                        resultDiv.innerHTML = '<span class="text-success-custom">Скидка ' + data.discount_percent + '% применена!</span>';
                        showToast('Промокод применён: скидка ' + data.discount_percent + '%', 'success');
                        haptic('light');
                        updateTariffPrices(data.discount_percent);
                        if (typeof onDiscount === 'function') onDiscount(data.discount_percent);
                    } else if (data.type === 'bonus_days') {
                        resultDiv.innerHTML = '<span class="text-gray">Начисляю бонусные дни...</span>';
                        return tmaFetch('/payment/apply-bonus-promo', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ code: code }),
                        }).then(function (applyResp) {
                            if (applyResp.ok) {
                                return applyResp.json().then(function (applyData) {
                                    resultDiv.innerHTML = '<span class="text-success-custom">Начислено ' + applyData.bonus_days + ' бонусных дней!</span>';
                                    showToast('+' + applyData.bonus_days + ' бонусных дней!', 'success');
                                    haptic('medium');
                                    setTimeout(function () { location.href = '/tma/'; }, 1500);
                                });
                            }
                            resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка при применении промокода</span>';
                        });
                    }
                } else {
                    tmaActivePromo = null;
                    resultDiv.innerHTML = '<span class="text-danger-custom">' + data.error + '</span>';
                    resetTariffPrices();
                }
            })
            .catch(function () {
                resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка соединения</span>';
            });
    }
    window.tmaApplyPromo = tmaApplyPromo;

    var refreshTariffQuoteUI = null;
    function updateTariffPrices() { if (refreshTariffQuoteUI) refreshTariffQuoteUI(); }
    function resetTariffPrices() { if (refreshTariffQuoteUI) refreshTariffQuoteUI(); }

    // ============================================================
    // Тарифы: выбор карточки → Telegram.WebApp.MainButton "Оплатить N ₽"
    // ============================================================

    function initTariffsScreen() {
        var cards = document.querySelectorAll('.tma-tariffs .tariff-card');
        var promoBtn = document.getElementById('promoApplyBtn');
        if (!cards.length && !promoBtn) return;
        var selected = null;
        var quoteVersion = 0;
        var hasMainButton = !!(tg && tg.MainButton);
        var slotBox = document.getElementById('slotSelector');
        var slotMax = slotBox ? parseInt(slotBox.dataset.slotMax || '0', 10) : 0;
        var baseLimit = slotBox ? parseInt(slotBox.dataset.baseLimit || '0', 10) : 0;
        var slots = slotBox ? parseInt(slotBox.dataset.currentSlots || '0', 10) : 0;

        function refreshSlotLabels(quote) {
            var label = document.getElementById('slotTotalLabel');
            if (label) label.textContent = baseLimit + slots;
            var costLabel = document.getElementById('slotCostLabel');
            if (costLabel) costLabel.textContent = quote && slots
                ? '+' + formatRubAmount(quote.slots_amount) + ' ₽ к тарифу'
                : (slots ? 'выберите тариф' : '');
        }
        async function refreshQuotes() {
            var version = ++quoteVersion;
            var quotedSlots = slots;
            var promo = tmaActivePromo;
            var errorMessage = null;
            if (selected) selected.quote = null;
            if (hasMainButton && selected) {
                tg.MainButton.setText('Рассчитываем цену…');
                tg.MainButton.show();
                tg.MainButton.disable();
            }
            await Promise.all(Array.from(cards).map(async function (card) {
                var payload = { tariff_name: card.dataset.tariffName, price: Number(card.dataset.tariffPrice), source: 'tma', extra_devices: quotedSlots };
                if (promo) payload.promo_code = promo.code;
                try {
                    var quote = await tmaPaymentQuote(payload);
                    if (version !== quoteVersion) return;
                    var priceEl = card.querySelector('.tariff-price');
                    if (priceEl) {
                        var struck = Number(quote.original_amount) > Number(quote.final_amount)
                            ? '<s class="text-gray" style="font-size:0.6em">' + formatRubAmount(quote.original_amount) + ' ₽</s> ' : '';
                        priceEl.innerHTML = struck + formatRubAmount(quote.final_amount) + ' <small class="fs-6">₽</small>';
                    }
                    if (selected && selected.card === card) {
                        selected.quote = quote;
                        refreshSlotLabels(quote);
                        if (hasMainButton) {
                            tg.MainButton.setText('Оплатить ' + formatRubAmount(quote.final_amount) + ' ₽');
                            tg.MainButton.show();
                            tg.MainButton.enable();
                        }
                    }
                } catch (error) {
                    if (version === quoteVersion) errorMessage = error.message;
                }
            }));
            if (errorMessage && version === quoteVersion) showToast(errorMessage, 'error');
        }
        refreshTariffQuoteUI = refreshQuotes;
        if (slotBox) slotBox.addEventListener('click', function (event) {
            var action = event.target.getAttribute('data-slot-action');
            if (!action) return;
            slots = Math.max(0, Math.min(slotMax, slots + (action === 'inc' ? 1 : -1)));
            haptic('light');
            refreshSlotLabels();
            refreshQuotes();
        });
        if (hasMainButton) {
            mainButtonHandler = function () {
                if (!selected || !selected.quote) return;
                var fakeBtn = document.createElement('button');
                fakeBtn.dataset.quotedAmount = selected.quote.final_amount;
                return tmaInitPayment(selected.name, selected.rawPrice, fakeBtn, tmaActivePromo, slots, undefined, selected.card.dataset.tariffDays);
            };
            tg.MainButton.onClick(mainButtonHandler);
        }
        cards.forEach(function (card) {
            card.addEventListener('click', function () {
                cards.forEach(function (c) { c.classList.remove('selected'); });
                card.classList.add('selected');
                haptic('light');
                selected = { name: card.dataset.tariffName, rawPrice: Number(card.dataset.tariffPrice), card: card, quote: null };
                refreshSlotLabels();
                refreshQuotes();
            });
        });
        if (promoBtn) promoBtn.addEventListener('click', function () { tmaApplyPromo(); });
        refreshSlotLabels();
        refreshQuotes();
    }

    // ============================================================
    // Оплата Telegram Stars (Фаза 3.2): POST /tma/payment/stars-invoice →
    // Telegram.WebApp.openInvoice. Не пересекается с MainButton-флоу Platega —
    // это отдельная кнопка "⭐ N" на карточке тарифа, клик по ней не выбирает
    // тариф для оплаты рублями (stopPropagation).
    // ============================================================

    function tmaPayWithStars(tariffId, btn) {
        if (!tg || !tg.openInvoice) {
            showToast('Оплата Stars недоступна в этом клиенте Telegram.', 'error');
            return;
        }
        btn.classList.add('btn-loading');
        tmaFetch('/tma/payment/stars-invoice', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tariff_id: parseInt(tariffId, 10) }),
        })
            .then(function (response) {
                if (!response.ok) {
                    if (response.status === 401) {
                        showToast('Сессия истекла, перезагрузите Mini App.', 'error');
                        return;
                    }
                    return response.json().catch(function () { return {}; }).then(function (data) {
                        showToast(data.detail || 'Не удалось создать счёт.', 'error');
                    });
                }
                return response.json().then(function (data) {
                    tg.openInvoice(data.invoice_url, function (status) {
                        if (status === 'paid') {
                            haptic('medium');
                            showToast('Оплата прошла успешно!', 'success');
                            setTimeout(function () { location.href = '/tma/'; }, 1500);
                        } else if (status === 'failed') {
                            showToast('Платёж не прошёл. Попробуйте ещё раз.', 'error');
                        } else if (status === 'cancelled') {
                            showToast('Оплата отменена.', 'info');
                        }
                    });
                });
            })
            .catch(function () {
                showToast('Ошибка соединения с сервером.', 'error');
            })
            .finally(function () {
                btn.classList.remove('btn-loading');
            });
    }

    function initStarsButtons() {
        var buttons = document.querySelectorAll('.tma-stars-btn');
        buttons.forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                haptic('light');
                tmaPayWithStars(btn.dataset.tariffId, btn);
            });
        });
    }

    // ============================================================
    // Активация триала (POST /profile/activate_trial): переиспользуем
    // существующий бэкенд-роут без изменений, но не даём ему увести
    // пользователя редиректом на сайтовый /profile/ — сами уходим на /tma/.
    // ============================================================

    function tmaActivateTrial(btn) {
        if (!btn) return;
        btn.classList.add('btn-loading');
        tmaFetch('/profile/activate_trial', { method: 'POST' })
            .then(function (res) {
                if (res.status === 401) {
                    showToast('Сессия истекла, перезагрузите Mini App.', 'error');
                    return;
                }
                // Успех: бэкенд (webapp/routers/dashboard.py, не трогаем) отвечает 303 → /profile/,
                // fetch сам идёт по редиректу, итоговый res.url = .../profile/. Ошибка (например,
                // триал уже использован) — рендерит форму заново БЕЗ редиректа, url остаётся
                // .../profile/activate_trial — по этому и отличаем один случай от другого.
                if (res.url && /\/profile\/$/.test(res.url)) {
                    haptic('medium');
                    location.href = '/tma/';
                } else {
                    showToast('Вы уже использовали пробный период или произошла ошибка.', 'warning');
                }
            })
            .catch(function () {
                showToast('Ошибка соединения с сервером.', 'error');
            })
            .finally(function () {
                btn.classList.remove('btn-loading');
            });
    }
    window.tmaActivateTrial = tmaActivateTrial;

    // ============================================================
    // Поддержка (Фаза 4): форма на /tma/support → POST /tma/support.
    // Ответ админа приходит пользователю в личку от бота как обычно
    // (см. webapp/routers/tma.py, tgbot/handlers/support.py).
    // ============================================================

    function tmaSendSupportMessage() {
        var textarea = document.getElementById('tmaSupportText');
        var resultDiv = document.getElementById('tmaSupportResult');
        var btn = document.getElementById('tmaSupportSendBtn');
        if (!textarea || !btn) return;

        var text = textarea.value.trim();
        if (!text) {
            if (resultDiv) resultDiv.innerHTML = '<span class="text-danger-custom">Введите сообщение</span>';
            return;
        }

        btn.classList.add('btn-loading');
        if (resultDiv) resultDiv.innerHTML = '';

        tmaFetch('/tma/support', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text }),
        })
            .then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    return { ok: response.ok, status: response.status, data: data };
                });
            })
            .then(function (result) {
                if (result.ok) {
                    textarea.value = '';
                    haptic('medium');
                    showToast('Сообщение отправлено!', 'success');
                    if (resultDiv) {
                        resultDiv.innerHTML = '<span class="text-success-custom">Отправлено! Ответ придёт в личные сообщения от бота.</span>';
                    }
                } else if (result.status === 401) {
                    showToast('Сессия истекла, перезагрузите Mini App.', 'error');
                } else if (result.status === 429) {
                    var msg = (result.data && result.data.detail) || 'Подождите перед отправкой следующего сообщения.';
                    if (resultDiv) resultDiv.innerHTML = '<span class="text-danger-custom">' + msg + '</span>';
                    showToast(msg, 'warning');
                } else {
                    var errMsg = (result.data && result.data.detail) || 'Не удалось отправить сообщение.';
                    if (resultDiv) resultDiv.innerHTML = '<span class="text-danger-custom">' + errMsg + '</span>';
                    showToast(errMsg, 'error');
                }
            })
            .catch(function () {
                showToast('Ошибка соединения с сервером.', 'error');
            })
            .finally(function () {
                btn.classList.remove('btn-loading');
            });
    }
    window.tmaSendSupportMessage = tmaSendSupportMessage;

    // ============================================================
    // OS detection для экрана /tma/import (та же логика, что detectOSAndSetLink на сайте)
    // ============================================================

    function detectOSAndSetLink() {
        var userAgent = navigator.userAgent || navigator.vendor || window.opera;
        var downloadBtn = document.getElementById('downloadBtn');
        var appDesc = document.getElementById('appDescription');
        var osIcon = document.getElementById('osIcon');
        var btnTextEl = document.getElementById('btnText');
        if (!downloadBtn) return;

        if (/android/i.test(userAgent)) {
            downloadBtn.href = 'https://play.google.com/store/apps/details?id=com.happproxy';
            appDesc.textContent = 'Для Android рекомендуем Happ.';
            osIcon.className = 'fab fa-android me-1';
            btnTextEl.textContent = 'Google Play';
        } else if (/iPad|iPhone|iPod/.test(userAgent) && !window.MSStream) {
            downloadBtn.href = 'https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643';
            appDesc.textContent = 'Для iOS рекомендуем Happ.';
            osIcon.className = 'fab fa-app-store-ios me-1';
            btnTextEl.textContent = 'App Store';
        } else if (/Win/i.test(userAgent)) {
            downloadBtn.href = 'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe';
            appDesc.textContent = 'Для Windows рекомендуем Happ.';
            osIcon.className = 'fab fa-windows me-1';
            btnTextEl.textContent = 'Скачать для Windows';
        } else if (/Mac/i.test(userAgent)) {
            downloadBtn.href = 'https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643';
            appDesc.textContent = 'Для macOS рекомендуем Happ.';
            osIcon.className = 'fab fa-apple me-1';
            btnTextEl.textContent = 'Скачать для macOS';
        } else {
            downloadBtn.href = 'https://play.google.com/store/apps/details?id=com.happproxy';
            appDesc.textContent = 'Выберите приложение для вашей ОС.';
            osIcon.className = 'fas fa-download me-1';
            btnTextEl.textContent = 'Скачать';
        }
    }

    // ============================================================
    // Реферальная ссылка: t.me/<bot>/<app>?startapp=<user_id> + шаринг через
    // Telegram.WebApp.openTelegramLink(t.me/share/url?...)
    // ============================================================

    function initShareRefLink() {
        var shareBtn = document.getElementById('tmaShareRefBtn');
        if (!shareBtn) return;
        shareBtn.addEventListener('click', function () {
            var link = shareBtn.dataset.refLink;
            var text = shareBtn.dataset.shareText || '';
            if (!link) return;
            var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(link) + '&text=' + encodeURIComponent(text);
            if (tg && tg.openTelegramLink) {
                tg.openTelegramLink(shareUrl);
            } else {
                window.open(shareUrl, '_blank');
            }
        });
    }

    // ============================================================
    // Страница подписки Remnawave: открываем во внешнем браузере
    // (Telegram.WebApp.openLink) — из webview Mini App happ:// deeplink
    // на самой sub-странице не отработает.
    // ============================================================

    function initOpenSubLink() {
        var btn = document.getElementById('tmaOpenSubBtn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var link = btn.dataset.subLink;
            if (!link) return;
            haptic('light');
            if (tg && tg.openLink) {
                tg.openLink(link);
            } else {
                window.open(link, '_blank');
            }
        });
    }

    // ============================================================
    // Докупка доп. устройств (экран «Мои устройства»)
    // ============================================================

    function initDeviceSlots() {
        var box = document.getElementById('buySlotsBox');
        if (!box) return;

        var unitPrice = parseInt(box.dataset.slotUnitPrice || '0', 10);
        var available = parseInt(box.dataset.slotAvailable || '1', 10);
        var count = 1;

        var countEl = document.getElementById('buySlotsCount');
        var priceEl = document.getElementById('buySlotsPrice');
        var buyBtn = document.getElementById('buySlotsBtn');

        function refresh() {
            if (countEl) countEl.textContent = count;
            if (priceEl) priceEl.textContent = formatRubAmount(unitPrice * count);
        }

        box.addEventListener('click', function (event) {
            var action = event.target.getAttribute('data-slot-action');
            if (!action) return;
            count = Math.max(1, Math.min(available, count + (action === 'inc' ? 1 : -1)));
            haptic('light');
            refresh();
        });

        if (!buyBtn) return;

        function submitSlots(allowCancelRetry) {
            buyBtn.classList.add('btn-loading');

            tmaFetch('/payment/device-slots', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slots: count, source: 'tma' }),
            })
                .then(function (response) {
                    if (response.ok) {
                        return response.json().then(function (data) {
                            if (tg && tg.openLink) {
                                tg.openLink(data.payment_url);
                            } else {
                                window.open(data.payment_url, '_blank');
                            }
                            showToast('Счёт открыт в браузере. Вернитесь сюда после оплаты.', 'info');
                        });
                    }
                    if (response.status === 401) {
                        showToast('Сессия истекла, перезагрузите Mini App.', 'error');
                        return null;
                    }
                    if (response.status === 409 && allowCancelRetry !== false) {
                        offerCancelPending(function () { submitSlots(false); });
                        return null;
                    }
                    // Текст отказа присылает сервер (нет подписки, потолок, счёт в работе).
                    return response.json().catch(function () { return {}; }).then(function (data) {
                        showToast(data.detail || 'Не удалось создать счёт. Попробуйте позже.', 'error');
                    });
                })
                .catch(function () {
                    showToast('Ошибка соединения с сервером.', 'error');
                })
                .finally(function () {
                    buyBtn.classList.remove('btn-loading');
                });
        }

        buyBtn.addEventListener('click', function () { submitSlots(true); });
    }

    // ============================================================
    // Автопродление: статус и отключение
    // ============================================================

    function renderAutorenewCard(data) {
        var card = document.getElementById('autorenewCard');
        var body = document.getElementById('autorenewBody');
        if (!card || !body) return;
        body.textContent = '';
        function line(text, cls) {
            var el = document.createElement('p');
            el.className = cls || 'mb-1';
            el.textContent = text;
            body.appendChild(el);
        }
        function button(text, handler, cls) {
            var el = document.createElement('button');
            el.type = 'button';
            el.className = cls || 'btn btn-brand-outline btn-sm mt-2';
            el.textContent = text;
            el.addEventListener('click', handler);
            body.appendChild(el);
        }
        if (!data || !data.status || ['active', 'pending', 'past_due', 'failed'].indexOf(data.status) < 0) {
            card.hidden = true;
            return;
        }
        card.hidden = false;
        var plan = (data.tariff_name || 'Тариф') + ' · ' + data.amount + ' ₽ каждые ' + (data.period_days || '') + ' дн.';
        if (data.status === 'active') {
            line('Включено: ' + plan);
            line('Следующее списание: ' + (data.next_charge_at || 'уточняется'), 'text-gray mb-1');
            button('Отключить автопродление', cancelAutorenew);
        } else if (data.status === 'pending') {
            line('Ждём привязку счёта в банке: ' + plan);
            if (data.payment_url) {
                button('Привязать счёт', function () {
                    if (tg && tg.openLink) tg.openLink(data.payment_url);
                    else window.open(data.payment_url, '_blank');
                }, 'btn btn-brand btn-sm mt-2 me-2');
            }
            button('Отменить', cancelAutorenew);
        } else if (data.status === 'past_due') {
            line('Автопродление остановлено: банк отклонил списание ' + data.amount + ' ₽. Продлите подписку вручную или подключите автопродление заново.');
        } else {
            line('Не удалось подключить автопродление. Деньги не списаны, попробуйте ещё раз.');
        }
    }

    function loadAutorenew() {
        if (!document.getElementById('autorenewCard')) return;
        tmaFetch('/payment/autorenew')
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (data) { if (data) renderAutorenewCard(data); })
            .catch(function () {});
    }

    function cancelAutorenew() {
        askConfirm('Отключить автопродление? Деньги перестанут списываться, оплаченный срок подписки сохранится.', function () {
            tmaFetch('/payment/autorenew/cancel', { method: 'POST' })
                .then(function (response) {
                    return response.json().catch(function () { return {}; }).then(function (data) {
                        if (response.ok) {
                            showToast('Автопродление отключено.', 'info');
                            loadAutorenew();
                        } else {
                            showToast(data.detail || 'Не удалось отключить автопродление.', 'error');
                        }
                    });
                })
                .catch(function () { showToast('Ошибка соединения с сервером.', 'error'); });
        });
    }

    // ============================================================
    // Init
    // ============================================================

    document.addEventListener('DOMContentLoaded', function () {
        loadAutorenew();
        resetMainButton();
        initAuthSplash();
        routeStartParam();
        initBackButton();
        initMoreSheet();
        detectOSAndSetLink();
        initTariffsScreen();
        initDeviceSlots();
        initStarsButtons();
        initShareRefLink();
        initOpenSubLink();
    });
})();
