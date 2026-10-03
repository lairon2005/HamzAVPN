-- Текущая схема HamzAVPN. Только структура: без серверов, ключей и пользовательских данных.


CREATE TABLE IF NOT EXISTS app_settings (
	key VARCHAR(64) NOT NULL, 
	value VARCHAR(255) NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (key)
)

;


CREATE TABLE IF NOT EXISTS channels (
	id BIGSERIAL NOT NULL, 
	channel_id BIGINT NOT NULL, 
	title VARCHAR NOT NULL, 
	invite_link VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (channel_id)
)

;


CREATE TABLE IF NOT EXISTS promo_codes (
	id BIGSERIAL NOT NULL, 
	code VARCHAR NOT NULL, 
	bonus_days INTEGER NOT NULL, 
	discount_percent INTEGER NOT NULL, 
	expire_date TIMESTAMP WITHOUT TIME ZONE, 
	max_uses INTEGER NOT NULL, 
	uses_left INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
)

;


CREATE TABLE IF NOT EXISTS tariffs (
	id BIGSERIAL NOT NULL, 
	name VARCHAR NOT NULL, 
	price FLOAT NOT NULL, 
	duration_days INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	data_limit_gb INTEGER, 
	loyalty_price FLOAT, 
	is_highlighted BOOLEAN NOT NULL, 
	price_stars INTEGER, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS users (
	user_id BIGSERIAL NOT NULL, 
	username VARCHAR, 
	full_name VARCHAR NOT NULL, 
	reg_date TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	subscription_end_date TIMESTAMP WITHOUT TIME ZONE, 
	subscription_data_limit_gb INTEGER, 
	vpn_username VARCHAR, 
	remnawave_uuid VARCHAR(36), 
	has_received_trial BOOLEAN NOT NULL, 
	referrer_id BIGINT, 
	referral_bonus_days INTEGER NOT NULL, 
	is_first_payment_made BOOLEAN NOT NULL, 
	support_topic_id INTEGER, 
	email VARCHAR(255), 
	password_hash VARCHAR(255), 
	reset_code VARCHAR(10), 
	reset_code_expire TIMESTAMP WITHOUT TIME ZONE, 
	verification_code VARCHAR(10), 
	verification_code_expire TIMESTAMP WITHOUT TIME ZONE, 
	is_email_verified BOOLEAN DEFAULT 'false' NOT NULL, 
	is_active BOOLEAN DEFAULT 'true' NOT NULL, 
	extra_devices INTEGER DEFAULT '0' NOT NULL, 
	PRIMARY KEY (user_id), 
	UNIQUE (vpn_username), 
	UNIQUE (remnawave_uuid), 
	FOREIGN KEY(referrer_id) REFERENCES users (user_id) ON DELETE SET NULL, 
	UNIQUE (email)
)

;


CREATE TABLE IF NOT EXISTS lifecycle_messages (
	id BIGSERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	series VARCHAR(32) NOT NULL, 
	step VARCHAR(64) NOT NULL, 
	sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_lifecycle_user_series_step UNIQUE (user_id, series, step), 
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS payments (
	id BIGSERIAL NOT NULL, 
	order_id VARCHAR(36), 
	provider_payment_id VARCHAR NOT NULL, 
	user_id BIGINT NOT NULL, 
	tariff_id BIGINT, 
	original_amount FLOAT NOT NULL, 
	final_amount FLOAT NOT NULL, 
	promo_code VARCHAR, 
	discount_percent INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	currency VARCHAR(3) DEFAULT 'RUB' NOT NULL, 
	payment_url VARCHAR, 
	duration_days INTEGER DEFAULT '0' NOT NULL, 
	data_limit_gb INTEGER, 
	last_checked_at TIMESTAMP WITHOUT TIME ZONE, 
	traffic_reset_done BOOLEAN DEFAULT 'false' NOT NULL, 
	sync_pending BOOLEAN DEFAULT 'false' NOT NULL, 
	referrer_id BIGINT, 
	source VARCHAR NOT NULL, 
	kind VARCHAR(16) DEFAULT 'subscription' NOT NULL, 
	extra_devices INTEGER DEFAULT '0' NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	telegram_payment_charge_id VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (order_id), 
	FOREIGN KEY(user_id) REFERENCES users (user_id), 
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id), 
	UNIQUE (telegram_payment_charge_id)
)

;

CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_provider_payment_id ON payments (provider_payment_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_user_pending ON payments (user_id) WHERE status = 'pending' AND source != 'stars';


CREATE TABLE IF NOT EXISTS used_promo_codes (
	id BIGSERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	promo_code_id BIGINT NOT NULL, 
	used_date TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_used_promo_user_code UNIQUE (user_id, promo_code_id), 
	FOREIGN KEY(user_id) REFERENCES users (user_id), 
	FOREIGN KEY(promo_code_id) REFERENCES promo_codes (id)
)

;
