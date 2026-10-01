/** @odoo-module **/

import publicWidget from "@web/legacy/js/public/public_widget";
import { jsonrpc } from "@web/core/network/rpc_service";

/**
 * Inline postal code validation against Envia.com on the website address
 * forms (/shop/address and /my/account). Purely informative: the server-side
 * checkout/portal validation enforces the same rule on submit.
 */
publicWidget.registry.EnviaZipValidation = publicWidget.Widget.extend({
    selector: 'form.checkout_autoformat, form[action="/my/account"]',
    events: {
        'focusout input[name="zip"], input[name="zipcode"]': '_onZipFocusout',
        'change select[name="country_id"]': '_onCountryChange',
    },

    _getZipInput() {
        return this.el.querySelector('input[name="zip"], input[name="zipcode"]');
    },

    _getCountryId() {
        const select = this.el.querySelector('select[name="country_id"]');
        return select ? parseInt(select.value) || 0 : 0;
    },

    _onZipFocusout() {
        this._validateZip();
    },

    _onCountryChange() {
        const input = this._getZipInput();
        if (input && input.value.trim()) {
            this._validateZip();
        }
    },

    async _validateZip() {
        const input = this._getZipInput();
        if (!input) {
            return;
        }
        const zip = input.value.trim();
        const countryId = this._getCountryId();
        this._clearFeedback(input);
        if (!zip || !countryId) {
            return;
        }
        const requestId = (this._lastRequestId = (this._lastRequestId || 0) + 1);
        try {
            const result = await jsonrpc('/delivery_envia/validate_zip', {
                country_id: countryId,
                zip: zip,
            });
            // Ignore stale responses (user kept typing/changed country)
            if (requestId !== this._lastRequestId || input.value.trim() !== zip) {
                return;
            }
            if (result && !result.valid && result.message) {
                this._showFeedback(input, result.message);
            }
        } catch {
            // Network/server issue: stay silent, submit validation will catch it
        }
    },

    _showFeedback(input, message) {
        input.classList.add('is-invalid');
        let feedback = input.parentNode.querySelector('.envia_zip_feedback');
        if (!feedback) {
            feedback = document.createElement('div');
            feedback.className = 'invalid-feedback envia_zip_feedback d-block';
            input.insertAdjacentElement('afterend', feedback);
        }
        feedback.textContent = message;
    },

    _clearFeedback(input) {
        input.classList.remove('is-invalid');
        const feedback = input.parentNode.querySelector('.envia_zip_feedback');
        if (feedback) {
            feedback.remove();
        }
    },
});

export default publicWidget.registry.EnviaZipValidation;
