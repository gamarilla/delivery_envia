{
    'name': 'Envia.com Shipping',
    'summary': 'Multi-carrier shipping integration via envia.com API',
    'category': 'Inventory/Delivery',
    'version': '17.0.1.2.0',
    'depends': ['stock_delivery', 'website', 'website_sale'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/envia_shipment_error_wizard_views.xml',
        'views/delivery_carrier_views.xml',
        'views/product_template_views.xml',
        'views/res_partner_views.xml',
        'views/stock_picking_views.xml',
        'views/tracking_templates.xml',
        'data/data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'delivery_envia/static/src/css/tracking.css',
            'delivery_envia/static/src/js/zip_validation.js',
        ],
    },
    'license': 'LGPL-3',
    'application': False,
    'installable': True,
}
