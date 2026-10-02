"""JavaScript module served by the plugin for the primary action button."""

IMPORT_ORDERS_JS = """
export async function importOrders({ inventreeContext }) {
    const api = inventreeContext.api;

    try {
        await api.post('/api/action/', {
            action: 'woocommerce-sync-orders',
            data: {}
        });
        alert('WooCommerce order import completed');
        window.location.reload();
    } catch (error) {
        const detail = error?.response?.data?.error || error?.message || error;
        console.error('WooCommerce order import failed:', error);
        alert(`WooCommerce order import failed: ${detail}`);
    }
}
"""
