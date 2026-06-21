import voluptuous as vol
from homeassistant import config_entries
from .const import DOMAIN

class BLEFingerprintConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce il flusso di configurazione UI per BLE Fingerprint."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Gestisce il primo passaggio quando l'utente clicca 'Aggiungi'."""
        # Evita che l'utente installi l'integrazione due volte per sbaglio
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # Crea l'integrazione senza chiedere opzioni aggiuntive per ora
            return self.async_create_entry(title="BLE Radar Fingerprint", data={})

        # Mostra una finestra vuota con solo il tasto "Invia"
        return self.async_show_form(step_id="user")
