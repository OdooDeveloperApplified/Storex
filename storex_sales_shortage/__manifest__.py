{
    'name': 'Storex Sales Shortage',
    'version': '18.0.1.0',
    'category': 'Sales',
    'author': 'Applified',
    'website': 'https://www.storex.com',
    'depends': ['sale_management', 'mrp', 'stock', 'storex_manufacture'],
    'data': [
        'security/ir.model.access.csv',
        'views/sale_order_views.xml',
        'report/sale_shortage_report.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
