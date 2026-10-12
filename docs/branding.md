# Оформление HamzAVPN

Основные цвета — белый и чёрный; нейтральные серые используются для текста и границ. Ошибки и успешные статусы сохраняют смысловые цвета.

Логотип: `webapp/static/img/hamzavpn-logo.png`, прозрачный PNG. Для тёмного фона используется CSS `filter: invert(1)`.

Логотип создан встроенным imagegen по предоставленному образцу. Промпт:

> Edit the supplied hamza ESIM logo. Preserve faithfully the lowercase connected flowing organic black hamza wordmark, horizontal signal line and three arcs beneath. Replace top E S I M with exact uppercase V P N, thin geometric sans serif, centered and spaced. Clean crisp contours, pure solid black, no texture or gradients. Transparent alpha background. Tight horizontal composition with modest padding, high resolution production logo, no mockup, no other elements.

После обновления файлов оформления меняйте версию в URL статических ресурсов, чтобы браузер получил новые изображения и стили.

Цены, сроки, трафик и число устройств отображаются из каталога и настроек сервиса. Перед созданием платежа сумма проверяется сервером.

Иконка вкладки: `webapp/static/img/hamzavpn-icon.png`. Промпт imagegen: Create a square favicon from the supplied logo. Only the recognisable lowercase flowing organic letter h, solid white centered on solid black background, enlarged to fill 75 percent of the square. Crisp clean shapes, no other letters or text, no extra symbols, no shadows or textures, strictly black and white. Single flat square icon.
