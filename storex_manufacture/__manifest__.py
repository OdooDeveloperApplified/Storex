{
    'name': 'Storex Manufacture',
    'version': '18.0.1.0',
    'category': '',
    'author': 'Applified',
    'website': 'https://www.storex.com',
    'depends': ['base','stock','product','mrp'],
    'data': [
       
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/storex_product_template.xml',
        'views/mrp_shortage_views.xml',
        'views/is_finished_product.xml',
        'report/shortage_report.xml',
        
    ],
    
    'assets': {},
    'installable': True,
    'auto_install': False,
}