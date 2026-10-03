// webapp/static/js/scripts.js

// ============================================================
// Toast System
// ============================================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast-item ${type}`;
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================
// State
// ============================================================

let activePromo = null;

// ============================================================
// Payment
// ============================================================

// Доп. устройства, выбранные на экране тарифов. Цена слотов не зависит от
// тарифа — только от его длительности, поэтому одно значение на все карточки.
let selectedSlots = 0;
let tariffRenderVersion = 0;

function formatRubAmount(value) {
    return Number(value).toFixed(2);
}

async function paymentQuote(payload) {
    const response = await fetch('/payment/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.detail || 'Не удалось рассчитать итоговую стоимость.');
        error.status = response.status;
        throw error;
    }
    return data;
}


function billingMonths(durationDays) {
    // Тот же расчёт, что на сервере (device_pricing.billing_months):
    // 365 дней → 12 месяцев, минимум один месяц.
    return Math.max(1, Math.round(durationDays / 30));
}

function initSlotSelector() {
    // Стартуем с уже оплаченных слотов: продление, в котором человек не трогал
    // счётчик, не должно снимать его доп. устройства.
    const box = document.getElementById('slotSelector');
    if (!box) return;
    selectedSlots = parseInt(box.dataset.currentSlots || '0', 10);
    updateSlotCount(0);
}

function updateSlotCount(delta) {
    const box = document.getElementById('slotSelector');
    if (!box) return;

    const max = parseInt(box.dataset.slotMax || '0', 10);
    const baseLimit = parseInt(box.dataset.baseLimit || '0', 10);

    selectedSlots = Math.max(0, Math.min(max, selectedSlots + delta));

    const label = document.getElementById('slotTotalLabel');
    if (label) label.textContent = baseLimit + selectedSlots;

    renderTariffPrices(activePromo ? activePromo.discount_percent : 0);
}

// Локальное состояние pending препятствует одновременному созданию двух счетов.
// Перед сменой способа оплаты пользователь подтверждает завершение старого заказа.
async function offerCancelPending(retry) {
    const ok = confirm(
        'У вас уже есть неоплаченный счёт.\n\n' +
        'Отменить его и выставить новый? Старую ссылку на оплату после этого ' +
        'открывать не нужно.'
    );
    if (!ok) return;

    try {
        const response = await fetch('/payment/cancel-pending', { method: 'POST' });
        if (response.ok) {
            showToast('Счёт отменён, выставляю новый…', 'info');
            await retry();
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else {
            showToast('Не удалось отменить счёт. Обновите страницу и попробуйте снова.', 'error');
        }
    } catch (error) {
        console.error('Cancel pending error:', error);
        showToast('Ошибка соединения с сервером.', 'error');
    }
}

async function initPayment(tariffName, price, btn, allowCancelRetry = true) {
    if (!btn) return;
    btn.classList.add('btn-loading');

    try {
        const payload = { tariff_name: tariffName, price: price, extra_devices: selectedSlots };
        if (activePromo) {
            payload.promo_code = activePromo.code;
        }

        const quote = await paymentQuote(payload);
        const priorTotal = btn.dataset.quotedAmount;
        if (priorTotal && formatRubAmount(priorTotal) !== formatRubAmount(quote.final_amount)) {
            if (!confirm('Итоговая стоимость обновилась: ' + formatRubAmount(quote.final_amount) + ' ₽. Перейти к оплате?')) return;
        }
        btn.dataset.quotedAmount = quote.final_amount;
        const label = btn.querySelector(".btn-text");
        if (label) label.textContent = "Оплатить " + formatRubAmount(quote.final_amount) + " ₽";
        const response = await fetch('/payment/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const data = await response.json();
            if (data.final_amount != null && formatRubAmount(data.final_amount) !== formatRubAmount(quote.final_amount)) {
                if (!confirm('Итог к оплате: ' + formatRubAmount(data.final_amount) + ' ₽. Открыть платёжную страницу?')) return;
            }
            window.location.href = data.payment_url;
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else if (response.status === 409) {
            // Один повтор: если и после отмены 409, дальше молча уходим в toast.
            if (allowCancelRetry) {
                await offerCancelPending(() => initPayment(tariffName, price, btn, false));
            } else {
                showToast("У вас уже есть неоплаченный счёт. Завершите оплату или попробуйте позже.", "warning");
            }
        } else {
            showToast("Ошибка при создании платежа. Попробуйте позже.", "error");
        }
    } catch (error) {
        console.error('Payment error:', error);
        if (error.status === 401) window.location.href = '/login';
        else showToast(error.message || "Ошибка соединения с сервером.", "error");
    } finally {
        btn.classList.remove('btn-loading');
    }
}

// ============================================================
// Device slots (докупка устройств в середине периода)
// ============================================================

let buySlotsCount = 1;

function updateBuySlots(delta) {
    const box = document.getElementById('buySlotsBox');
    if (!box) return;

    const available = parseInt(box.dataset.slotAvailable || '1', 10);
    const unitPrice = parseInt(box.dataset.slotUnitPrice || '0', 10);

    buySlotsCount = Math.max(1, Math.min(available, buySlotsCount + delta));

    const countEl = document.getElementById('buySlotsCount');
    if (countEl) countEl.textContent = buySlotsCount;

    const priceEl = document.getElementById('buySlotsPrice');
    if (priceEl) priceEl.textContent = formatRubAmount(unitPrice * buySlotsCount);
}

async function buyDeviceSlots(btn, allowCancelRetry = true) {
    if (!btn) return;
    btn.classList.add('btn-loading');

    try {
        const response = await fetch('/payment/device-slots', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slots: buySlotsCount })
        });

        if (response.ok) {
            const data = await response.json();
            window.location.href = data.payment_url;
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else if (response.status === 409 && allowCancelRetry) {
            await offerCancelPending(() => buyDeviceSlots(btn, false));
        } else {
            // Сервер присылает готовый текст (нет подписки, потолок, занятый счёт).
            const data = await response.json().catch(() => ({}));
            showToast(data.detail || "Не удалось создать счёт. Попробуйте позже.", "error");
        }
    } catch (error) {
        console.error('Device slots error:', error);
        showToast("Ошибка соединения с сервером.", "error");
    } finally {
        btn.classList.remove('btn-loading');
    }
}

// ============================================================
// Promo Code
// ============================================================

async function applyPromo() {
    const input = document.getElementById('promoInput');
    const resultDiv = document.getElementById('promoResult');
    const code = input.value.trim();

    if (!code) {
        activePromo = null;
        resultDiv.textContent = 'Промокод отменён.';
        resetTariffPrices();
        return;
    }

    resultDiv.innerHTML = '<span class="text-gray">Проверяю...</span>';

    try {
        const response = await fetch('/payment/validate-promo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });

        const data = await response.json();

        if (data.valid) {
            if (data.type === 'discount') {
                activePromo = { code: data.code, discount_percent: data.discount_percent };
                resultDiv.innerHTML = `<span class="text-success-custom">Скидка ${data.discount_percent}% применена!</span>`;
                showToast(`Промокод применён: скидка ${data.discount_percent}%`, 'success');
                updateTariffPrices(data.discount_percent);
            } else if (data.type === 'bonus_days') {
                resultDiv.innerHTML = '<span class="text-gray">Применяю бонусные дни...</span>';
                const applyResp = await fetch('/payment/apply-bonus-promo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: code })
                });
                if (applyResp.ok) {
                    const applyData = await applyResp.json();
                    resultDiv.innerHTML = `<span class="text-success-custom">Начислено ${applyData.bonus_days} бонусных дней!</span>`;
                    showToast(`+${applyData.bonus_days} бонусных дней!`, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка при применении промокода</span>';
                }
            }
        } else {
            activePromo = null;
            resultDiv.innerHTML = `<span class="text-danger-custom">${data.error}</span>`;
            resetTariffPrices();
        }
    } catch (error) {
        console.error('Promo error:', error);
        resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка соединения</span>';
    }
}

// Цена карточки зависит сразу от двух вещей — промокода и количества доп.
// устройств, — поэтому рисуется в одном месте. Иначе промокод затирал бы
// разметку, на которую опирается счётчик устройств, и брал бы за «исходную»
// цену уже посчитанную сумму со слотами (а скидка на слоты не действует).
async function renderTariffPrices(discountPercent) {
    const cards = document.querySelectorAll('#tariffCards .tariff-card[data-tariff-price]');
    if (!cards.length) {
        legacyUpdateTariffPrices(discountPercent);
        return;
    }
    const version = ++tariffRenderVersion;
    const slots = selectedSlots;
    const box = document.getElementById('slotSelector');
    const baseLimit = box ? parseInt(box.dataset.baseLimit || '0', 10) : 0;
    const promo = activePromo;
    let quoteError = null;
    await Promise.all(Array.from(cards).map(async (card) => {
        const button = card.querySelector('[data-payment-tariff]');
        if (button) button.disabled = true;
        const payload = { tariff_name: card.dataset.tariffName, price: Number(card.dataset.tariffPrice), extra_devices: slots };
        if (promo) payload.promo_code = promo.code;
        try {
            const quote = await paymentQuote(payload);
            if (version !== tariffRenderVersion) return;
            const priceEl = card.querySelector('.tariff-price');
            if (priceEl) {
                const struck = Number(quote.original_amount) > Number(quote.final_amount)
                    ? '<s class="text-gray" style="font-size:0.6em">' + formatRubAmount(quote.original_amount) + ' ₽</s> ' : '';
                priceEl.innerHTML = struck + '<span class="tariff-price-value">' + formatRubAmount(quote.final_amount) + '</span> <small class="fs-6">₽</small>';
            }
            const devicesEl = card.querySelector('.tariff-devices-line');
            if (devicesEl) {
                const extra = slots ? ' <span class="text-gray small">(+' + formatRubAmount(quote.slots_amount) + ' ₽)</span>' : '';
                devicesEl.innerHTML = '<i class="fas fa-check"></i> ' + (baseLimit + slots) + ' устройств' + extra;
            }
            if (button) {
                button.dataset.quotedAmount = quote.final_amount;
                const label = button.querySelector(".btn-text");
                if (label) label.textContent = "Оплатить " + formatRubAmount(quote.final_amount) + " ₽";
                button.disabled = false;
            }
        } catch (error) {
            if (version === tariffRenderVersion) quoteError = error;
        }
    }));
    if (quoteError && version === tariffRenderVersion) showToast(quoteError.message, 'error');
}

function legacyUpdateTariffPrices(discountPercent) {
    document.querySelectorAll('.tariff-price').forEach(el => {
        const originalPrice = parseFloat(el.dataset.originalPrice || el.textContent);
        if (!el.dataset.originalPrice) {
            el.dataset.originalPrice = originalPrice;
        }
        if (!discountPercent) {
            el.innerHTML = `${formatRubAmount(originalPrice)} <small class="fs-6">₽</small>`;
            return;
        }
        const newPrice = Math.round(originalPrice * (1 - discountPercent / 100) * 100) / 100;
        el.innerHTML = `<s class="text-gray" style="font-size:0.6em">${formatRubAmount(originalPrice)} ₽</s> ${formatRubAmount(newPrice)} <small class="fs-6">₽</small>`;
    });
}

function updateTariffPrices(discountPercent) {
    renderTariffPrices(discountPercent);
}

function resetTariffPrices() {
    renderTariffPrices(0);
}

// ============================================================
// Copy Functions
// ============================================================

function copyLink() {
    const input = document.getElementById("subLink");
    if (!input) return;
    navigator.clipboard.writeText(input.value).then(() => {
        showToast("Ключ скопирован!", "success");
    });
}

function initRefLink() {
    const input = document.getElementById("refLink");
    if (!input) return;
    const ref = input.dataset.ref;
    if (ref) input.value = window.location.origin + "/register?ref=" + ref;
}

function copyRefLink() {
    const input = document.getElementById("refLink");
    if (!input) return;
    navigator.clipboard.writeText(input.value).then(() => {
        showToast("Реферальная ссылка скопирована!", "success");
    });
}

// ============================================================
// OS Detection
// ============================================================

function detectOSAndSetLink() {
    const userAgent = navigator.userAgent || navigator.vendor || window.opera;
    const downloadBtn = document.getElementById("downloadBtn");
    const appDesc = document.getElementById("appDescription");
    const osIcon = document.getElementById("osIcon");
    const btnTextEl = document.getElementById("btnText");

    if (!downloadBtn) return;

    if (/android/i.test(userAgent)) {
        downloadBtn.href = "https://play.google.com/store/apps/details?id=com.happproxy";
        appDesc.textContent = "Для Android рекомендуем Happ.";
        osIcon.className = "fab fa-android me-1";
        btnTextEl.textContent = "Google Play";
    } else if (/iPad|iPhone|iPod/.test(userAgent) && !window.MSStream) {
        downloadBtn.href = "https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643";
        appDesc.textContent = "Для iOS рекомендуем Happ.";
        osIcon.className = "fab fa-app-store-ios me-1";
        btnTextEl.textContent = "App Store";
    } else if (/Win/i.test(userAgent)) {
        downloadBtn.href = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe";
        appDesc.textContent = "Для Windows рекомендуем Happ.";
        osIcon.className = "fab fa-windows me-1";
        btnTextEl.textContent = "Скачать для Windows";
    } else if (/Mac/i.test(userAgent)) {
        downloadBtn.href = "https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643";
        appDesc.textContent = "Для macOS рекомендуем Happ.";
        osIcon.className = "fab fa-apple me-1";
        btnTextEl.textContent = "Скачать для macOS";
    } else {
        downloadBtn.href = "https://play.google.com/store/apps/details?id=com.happproxy";
        appDesc.textContent = "Выберите приложение для вашей ОС.";
        osIcon.className = "fas fa-download me-1";
        btnTextEl.textContent = "Скачать";
    }
}

// ============================================================
// Password Strength Indicator
// ============================================================

function initPasswordStrength() {
    const passwordInput = document.getElementById('registerPassword');
    const strengthBar = document.getElementById('passwordStrengthBar');
    if (!passwordInput || !strengthBar) return;

    passwordInput.addEventListener('input', function() {
        const val = this.value;
        strengthBar.className = 'bar';

        if (val.length === 0) {
            strengthBar.style.width = '0';
            return;
        }
        if (val.length < 6) {
            strengthBar.classList.add('weak');
        } else if (val.length < 10 || !/[A-Z]/.test(val) || !/[0-9]/.test(val)) {
            strengthBar.classList.add('medium');
        } else {
            strengthBar.classList.add('strong');
        }
    });
}

// ============================================================
// Form Loading State
// ============================================================

function initFormLoading() {
    document.querySelectorAll('form').forEach(form => {
        form.addEventListener('submit', function() {
            const btn = form.querySelector('button[type="submit"]');
            if (btn && !btn.classList.contains('btn-link')) {
                btn.classList.add('btn-loading');
            }
        });
    });
}

// ============================================================
// Scroll Animations (IntersectionObserver)
// ============================================================

function initScrollAnimations() {
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.style.animationPlayState = 'running';
                observer.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.animate-in').forEach(el => {
        el.style.animationPlayState = 'paused';
        observer.observe(el);
    });
}

// ============================================================
// Init
// ============================================================

document.addEventListener("DOMContentLoaded", function() {
    detectOSAndSetLink();
    initRefLink();
    initPasswordStrength();
    initFormLoading();
    initScrollAnimations();
    initSlotSelector();
});
