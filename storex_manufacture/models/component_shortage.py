import logging
from odoo import fields, models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class MrpShortageLine(models.Model):
    _name = 'mrp.shortage.line'
    _description = 'Manufacturing Shortage Line'
    _order = 'level'

    production_id = fields.Many2one('mrp.production', ondelete='cascade', string='Manufacturing Order')
    product_id = fields.Many2one('product.product', string='Component')
    required_qty = fields.Float(string='Required Quantity')
    available_qty = fields.Float(string='Available Quantity')
    shortage_qty = fields.Float(string='Shortage Quantity')
    is_raw_material = fields.Boolean(string='Is Raw Material', default=False)
    component_path = fields.Char(string='Component Path', help='Full path in BOM hierarchy')
    level = fields.Integer(string='Level', default=0, help='Depth in BOM structure')
    location_details = fields.Text(string='Location Details', help='Stock locations with quantities')
    primary_location = fields.Char(string='Primary Location', help='Main location where component is stored')


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    shortage_line_ids = fields.One2many(
        'mrp.shortage.line',
        'production_id',
        string='Component Shortages',
        readonly=True
    )

    has_recursive_shortage = fields.Boolean(
        string='Has Component Shortages',
        readonly=True,
        default=False
    )

    def action_print_shortage_report(self):
        """Generate PDF report for component shortage analysis"""
        self.ensure_one()
        
        # Ensure shortages are up to date
        self._compute_shortage_lines_manual()
    
        # Return report action
        return self.env.ref('storex_manufacture.action_report_mrp_shortage').report_action(self)
    
    def action_check_shortages(self):
        """Manual action to check and update component shortages"""
        self.ensure_one()
        try:
            self._compute_shortage_lines_manual()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Shortages Updated'),
                    'message': _('Component shortages have been recalculated.'),
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            raise UserError(_("Error checking shortages: %s") % str(e))

    def _get_product_location_details(self, product):
        """Get detailed location information for a product (only location names, no quantities)"""
        if not product:
            return 0.0, '', ''
        
        # Get ALL internal locations
        internal_locations = self.env['stock.location'].search([
            ('usage', '=', 'internal')
        ])
        
        if not internal_locations:
            _logger.warning(f"No internal locations found")
            return 0.0, '', ''
        
        total_free_qty = 0.0
        location_names = []
        primary_location = ''
        
        # Get stock quant information
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', product.id),
            ('location_id', 'in', internal_locations.ids),
            ('quantity', '>', 0)
        ])
        
        # Collect unique location names (no quantities)
        seen_locations = set()
        for quant in quants:
            location_name = quant.location_id.name
            quantity = quant.quantity
            if quantity > 0:
                total_free_qty += quantity
                # Only add location name if not already added
                if location_name not in seen_locations:
                    location_names.append(location_name)
                    seen_locations.add(location_name)
                    if not primary_location:
                        primary_location = location_name
        
        # Join location names with commas
        location_info = ', '.join(location_names) if location_names else 'No stock available'
        
        # Determine primary location category for grouping
        if primary_location:
            primary_location = primary_location.lower()
        else:
            primary_location = 'no_stock'
        
        _logger.info(f"Product: {product.name}, Total: {total_free_qty}, Locations: {location_info}")
        
        return total_free_qty, location_info, primary_location

    def _compute_shortage_lines_manual(self):
        """Manual computation of shortage lines"""
        for mo in self:
            # Clear existing lines
            mo.shortage_line_ids.unlink()
            
            if not mo.bom_id:
                mo.has_recursive_shortage = False
                continue
            
            try:
                all_components = []
                
                # Get all components from BOM hierarchy
                self._get_all_components(
                    mo.bom_id, 
                    mo.product_qty, 
                    all_components, 
                    parent_path=mo.product_id.name
                )
                
                _logger.info(f"Found {len(all_components)} total components for MO {mo.id}")
                
                # Create shortage lines for all components
                for comp_data in all_components:
                    available_qty, location_info, primary_location = self._get_product_location_details(comp_data['product'])
                    
                    _logger.info(f"Component: {comp_data['product'].name}, Required: {comp_data['required_qty']}, Available: {available_qty}")
                    
                    # Determine if raw material (no BOM)
                    is_raw = not self._has_bom(comp_data['product'])
                    
                    # Create shortage line
                    self.env['mrp.shortage.line'].create({
                        'production_id': mo.id,
                        'product_id': comp_data['product'].id,
                        'required_qty': comp_data['required_qty'],
                        'available_qty': available_qty,
                        'shortage_qty': max(0, comp_data['required_qty'] - available_qty),
                        'is_raw_material': is_raw,
                        'component_path': comp_data['path'],
                        'level': comp_data['level'],
                        'location_details': location_info,
                        'primary_location': primary_location,
                    })
                
                mo.has_recursive_shortage = any(
                    line.shortage_qty > 0 for line in mo.shortage_line_ids
                )
                
                _logger.info(f"Created {len(all_components)} component lines for MO {mo.id}")
                
            except Exception as e:
                _logger.error(f"Error computing shortages for MO {mo.id}: {str(e)}", exc_info=True)
                mo.has_recursive_shortage = False
                raise UserError(_("Error computing shortages: %s") % str(e))

    def _get_all_components(self, bom, quantity, result, level=0, parent_path="", processed=None):
        """Get all components from BOM hierarchy"""
        if processed is None:
            processed = set()
        
        if not bom:
            return
        
        bom_qty = bom.product_qty if bom.product_qty > 0 else 1.0
        multiplier = quantity / bom_qty
        
        for bom_line in bom.bom_line_ids:
            component = bom_line.product_id
            if not component:
                continue
            
            # Skip if already processed to avoid duplicates
            if component.id in processed:
                _logger.info(f"Skipping duplicate component: {component.name}")
                continue
            
            component_required = bom_line.product_qty * multiplier
            component_path = f"{parent_path} > {component.name}" if parent_path else component.name
            current_level = level + 1
            
            # Add the component
            result.append({
                'product': component,
                'required_qty': component_required,
                'path': component_path,
                'level': current_level,
            })
            
            # Mark as processed
            processed.add(component.id)
            
            # If component has its own BOM, get its sub-components
            component_bom = self._get_bom_for_product(component)
            if component_bom:
                _logger.info(f"Component {component.name} has BOM, recursing...")
                self._get_all_components(
                    component_bom,
                    component_required,
                    result,
                    current_level,
                    component_path,
                    processed.copy()
                )
            else:
                _logger.info(f"Component {component.name} has no BOM (raw material)")

    def _get_bom_for_product(self, product):
        """Safely get BOM for a product"""
        if not product:
            return None
        
        try:
            # Search for active BOM
            bom = self.env['mrp.bom'].sudo().search([
                ('product_tmpl_id', '=', product.product_tmpl_id.id),
                ('type', '=', 'normal'),
                ('active', '=', True)
            ], limit=1)
            
            if bom:
                _logger.info(f"Found BOM for product: {product.name}")
            else:
                _logger.info(f"No BOM found for product: {product.name}")
            
            return bom
        except Exception as e:
            _logger.warning(f"Error finding BOM for {product.name}: {str(e)}")
            return None

    def _has_bom(self, product):
        """Check if a product has a BOM"""
        if not product:
            return False
        return bool(self._get_bom_for_product(product))

    @api.model_create_multi
    def create(self, vals_list):
        """Override create to compute shortages after creation"""
        records = super().create(vals_list)
        for record in records:
            if record.bom_id:
                try:
                    record._compute_shortage_lines_manual()
                except Exception as e:
                    _logger.error(f"Error computing shortages on create for MO {record.id}: {str(e)}")
        return records

    def write(self, vals):
        """Override write to recompute shortages when relevant fields change"""
        result = super().write(vals)
        
        if any(field in vals for field in ['bom_id', 'product_id', 'product_qty']):
            for record in self:
                if record.bom_id:
                    try:
                        record._compute_shortage_lines_manual()
                    except Exception as e:
                        _logger.error(f"Error computing shortages on write for MO {record.id}: {str(e)}")
                else:
                    record.shortage_line_ids.unlink()
                    record.has_recursive_shortage = False
        
        return result