from odoo import fields, models, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

# All diagnostic logging added for this feature is tagged with this prefix,
# so you can find it quickly with: grep "MFG_QTY" odoo-server.log
# Logged at WARNING level on purpose, so it shows up even if your odoo.conf
# log_level is set above 'info' (e.g. 'warn').
LOG_TAG = "[MFG_QTY]"


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # Change this from related field to a regular float field
    threshold_qty = fields.Float(
        string='Threshold Quantity',
        help='Minimum threshold quantity for this product variant.'
    )

    alert_state = fields.Boolean(
        string='Low Stock Alert',
        compute='_compute_alert_state',
        store=False
    )

    @api.depends('free_qty', 'threshold_qty')
    def _compute_alert_state(self):
        for rec in self:
            rec.alert_state = rec.free_qty <= rec.threshold_qty

    @api.model
    def cron_auto_create_manufacturing_orders(self):

        mrp_production = self.env['mrp.production']
        mrp_bom = self.env['mrp.bom']

        # Get products having threshold
        products = self.search([
            ('threshold_qty', '>', 0),
            ('active', '=', True),
        ])

        for product in products:

            # Skip if stock is above threshold
            if product.free_qty > product.threshold_qty:
                continue

            # Find BOM - Correct way for Odoo 18
            # _bom_find expects a product recordset as first argument
            bom_dict = mrp_bom._bom_find(product)

            # Get the BOM for this specific product
            bom = bom_dict.get(product)

            if not bom:
                continue

            # Avoid duplicate MO
            existing_mo = mrp_production.search([
                ('product_id', '=', product.id),
                ('state', 'in', ['draft', 'confirmed', 'progress', 'to_close']),
            ], limit=1)

            if existing_mo:
                continue

            # Qty to produce
            qty_to_produce = (product.threshold_qty - product.free_qty) + 1

            if qty_to_produce <= 0:
                continue

            mo_vals = {
                'product_id': product.id,
                'product_tmpl_id': product.product_tmpl_id.id,
                'product_qty': qty_to_produce,
                'bom_id': bom.id,
                'product_uom_id': product.uom_id.id,
                'origin': _('Auto Generated from Threshold Cron'),
            }

            mo = mrp_production.create(mo_vals)

            # Confirm MO
            mo.action_confirm()

    # ------------------------------------------------------------------
    # Manufacturable quantity
    # ------------------------------------------------------------------
    # store=True: the value is persisted, and gets recomputed eagerly the
    # moment something that affects it changes - driven explicitly by the
    # StockQuant override at the bottom of this file, NOT by depending on
    # free_qty/qty_available. Those quantity fields are computed from
    # stock_move_ids with @api.depends_context (location/lot/owner/date
    # filters), and that kind of context-dependent compute does not reliably
    # propagate through a multi-hop cross-model @api.depends chain to a
    # stored field elsewhere - which is why earlier versions of this field
    # appeared to update "by luck" (e.g. right after a module upgrade's
    # full recompute) rather than consistently on every stock change.
    manufacturable_qty = fields.Float(
        string='Manufacturable Quantity',
        compute='_compute_manufacturable_qty',
        store=True,
        help='Maximum number of units that can be manufactured based on available component stock'
    )

    manufacturable_qty_uom = fields.Char(
        string='Manufacturable UOM',
        compute='_compute_manufacturable_qty',
        store=True,
        help='Unit of measure for manufacturable quantity'
    )
    is_finished_product = fields.Boolean(string='Is Finished Product')

    @api.depends(
        # BOM recipe itself changing (qty produced per batch, line quantities)
        'product_tmpl_id.bom_ids.product_qty',
        'product_tmpl_id.bom_ids.bom_line_ids.product_qty',
        # Recursive: a sub-assembly component's own manufacturable_qty
        # changing (covers multi-level BOMs). manufacturable_qty is a plain
        # stored field with a normal depends graph, so this hop IS reliable
        # - unlike free_qty, which is deliberately NOT listed here.
        'product_tmpl_id.bom_ids.bom_line_ids.product_id.manufacturable_qty',
    )
    def _compute_manufacturable_qty(self):
        """Compute how many units of this product can be manufactured based on available components"""
        for product in self:
            # Get the BOM for this product
            bom = self.env['mrp.bom']._bom_find(product).get(product)

            if not bom:
                _logger.warning(
                    "%s _compute_manufacturable_qty: no BOM found for %s (id=%s) -> setting 0",
                    LOG_TAG, product.display_name, product.id
                )
                product.manufacturable_qty = 0.0
                product.manufacturable_qty_uom = ''
                continue

            # Calculate manufacturable quantity based on BOM components
            manufacturable_qty = self._calculate_manufacturable_qty_from_bom(product, bom)

            _logger.warning(
                "%s _compute_manufacturable_qty: %s (id=%s) using BOM '%s' -> manufacturable_qty = %s",
                LOG_TAG, product.display_name, product.id, bom.display_name, manufacturable_qty
            )

            product.manufacturable_qty = manufacturable_qty
            product.manufacturable_qty_uom = product.uom_id.name

    def _calculate_manufacturable_qty_from_bom(self, product, bom):
        """Calculate how many units can be manufactured based on component availability"""
        if not bom or not bom.bom_line_ids:
            return 0.0

        # Get all internal locations
        internal_locations = self.env['stock.location'].search([
            ('usage', '=', 'internal')
        ])

        if not internal_locations:
            _logger.warning("No internal locations found for manufacturable calculation")
            return 0.0

        # Calculate maximum producible quantity for each component
        max_producible = float('inf')

        for line in bom.bom_line_ids:
            component = line.product_id
            if not component:
                continue

            # Get available quantity of this component
            available_qty = 0.0
            quants = self.env['stock.quant'].sudo().search([
                ('product_id', '=', component.id),
                ('location_id', 'in', internal_locations.ids),
                ('quantity', '>', 0)
            ])

            for quant in quants:
                available_qty += quant.quantity

            # Calculate how many finished products can be made with this component
            bom_qty = bom.product_qty if bom.product_qty > 0 else 1.0
            component_required_per_unit = line.product_qty / bom_qty

            if component_required_per_unit > 0:
                possible_from_component = available_qty / component_required_per_unit
                max_producible = min(max_producible, possible_from_component)

            _logger.info(f"Component: {component.name}, Available: {available_qty}, "
                        f"Required per unit: {component_required_per_unit}, "
                        f"Possible: {possible_from_component if component_required_per_unit > 0 else 0}")
            
        ################# Uncomment this block to enable recursive checking of sub-components with their own BOMs: starts ####################

        # If we have components with BOMs, we need to recursively check sub-components
        # for line in bom.bom_line_ids:
        #     component = line.product_id
        #     if not component:
        #         continue

        #     # Check if component has its own BOM
        #     component_bom = self._get_bom_for_product(component)
        #     if component_bom:
        #         # Recursively calculate manufacturable quantity for this component
        #         component_manufacturable = self._calculate_manufacturable_qty_from_bom(
        #             component, component_bom
        #         )

        #         # Adjust based on how many of this component are needed per finished product
        #         bom_qty = bom.product_qty if bom.product_qty > 0 else 1.0
        #         needed_per_finished = line.product_qty / bom_qty

        #         if needed_per_finished > 0:
        #             possible_from_sub = component_manufacturable / needed_per_finished
        #             max_producible = min(max_producible, possible_from_sub)

        #         _logger.info(f"Component {component.name} has BOM. "
        #                     f"Manufacturable sub-components: {component_manufacturable}, "
        #                     f"Needed per finished: {needed_per_finished}, "
        #                     f"Possible: {possible_from_sub if needed_per_finished > 0 else 0}")
        
        ################# Uncomment this block to enable recursive checking of sub-components with their own BOMs: ends ####################

        # If max_producible is still infinity, no components found or all have 0 requirement
        if max_producible == float('inf'):
            return 0.0

        # Return the maximum producible quantity (floor to avoid partial units)
        return max_producible

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

    def _recompute_manufacturable_for_components(self):
        """Given a recordset of component products (self), find every product
        whose BOM uses one of them as a component - at any level - and force
        their manufacturable_qty to recompute and persist.

        This is called explicitly from StockQuant.write/create below, since
        free_qty/qty_available changes do not reliably propagate through the
        standard @api.depends trigger graph. Walking and recomputing
        level-by-level here is what actually makes manufacturable_qty
        reactive to real stock changes - including for products with
        multiple variants sharing one template-level BOM.

        All steps are logged at WARNING level under the MFG_QTY tag so you
        can confirm exactly what was found and updated by checking the
        server log, without needing to run anything in odoo shell.
        """
        if not self:
            return

        _logger.warning(
            "%s _recompute_manufacturable_for_components: triggered by component(s) %s",
            LOG_TAG, self.mapped(lambda p: f"{p.display_name} (id={p.id}, free_qty={p.free_qty})")
        )

        to_check = self
        seen_finished = self.env['product.product']
        level = 0

        # Safety cap on levels walked upward, in case of unexpectedly deep
        # or (mis-configured, circular) BOM structures.
        for _ in range(20):
            level += 1

            if not to_check:
                _logger.warning("%s Level %s: nothing left to check, stopping.", LOG_TAG, level)
                break

            bom_lines = self.env['mrp.bom.line'].sudo().search([
                ('product_id', 'in', to_check.ids)
            ])

            _logger.warning(
                "%s Level %s: checking component(s) %s -> found %s BOM line(s)",
                LOG_TAG, level, to_check.mapped('display_name'), len(bom_lines)
            )

            if not bom_lines:
                _logger.warning("%s Level %s: no BOM uses these as a component, stopping.", LOG_TAG, level)
                break

            boms = bom_lines.bom_id

            # Template-level BOMs (no specific variant set) apply to every
            # variant of that template.
            template_level_boms = boms.filtered(lambda b: not b.product_id)
            templates = template_level_boms.product_tmpl_id
            finished = self.env['product.product'].sudo().search([
                ('product_tmpl_id', 'in', templates.ids)
            ])
            # Variant-specific BOMs apply only to that one variant.
            variant_level_boms = boms.filtered(lambda b: b.product_id)
            finished |= variant_level_boms.product_id

            _logger.warning(
                "%s Level %s: BOM(s) found: %s | template-level BOMs apply to templates: %s "
                "(-> all variants: %s) | variant-specific BOMs apply to: %s",
                LOG_TAG, level,
                boms.mapped('display_name'),
                templates.mapped('display_name'),
                finished.mapped('display_name'),
                variant_level_boms.product_id.mapped('display_name'),
            )

            # Drop anything already processed this pass, to avoid loops on
            # circular/self-referencing BOM data.
            finished = finished - seen_finished
            if not finished:
                _logger.warning(
                    "%s Level %s: all matching finished products already processed, stopping.",
                    LOG_TAG, level
                )
                break

            before = {p.id: p.manufacturable_qty for p in finished}
            finished._compute_manufacturable_qty()
            after = [(p.display_name, before.get(p.id), p.manufacturable_qty) for p in finished]

            _logger.warning(
                "%s Level %s: recomputed manufacturable_qty (name, old, new): %s",
                LOG_TAG, level, after
            )

            seen_finished |= finished
            to_check = finished

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def write(self, vals):
        res = super().write(vals)
        if 'quantity' in vals or 'reserved_quantity' in vals:
            products = self.product_id
            _logger.warning(
                "%s stock.quant.write touched %s for product(s): %s",
                LOG_TAG,
                [k for k in ('quantity', 'reserved_quantity') if k in vals],
                products.mapped('display_name'),
            )
            products._recompute_manufacturable_for_components()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        products = quants.product_id
        if products:
            _logger.warning(
                "%s stock.quant.create for product(s): %s",
                LOG_TAG, products.mapped('display_name')
            )
            products._recompute_manufacturable_for_components()
        return quants

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Keep this field but make it as a default value for variants
    threshold_qty = fields.Float(
        string='Default Threshold Quantity',
        help='Default threshold quantity for variants. Can be overridden per variant.'
    )

    alert_state = fields.Boolean(
        string='Low Stock Alert',
        compute='_compute_alert_state',
        store=False
    )

    @api.depends('qty_available', 'threshold_qty')
    def _compute_alert_state(self):
        for rec in self:
            rec.alert_state = rec.qty_available <= rec.threshold_qty

    @api.model_create_multi
    def create(self, vals_list):
        """When creating a new product template, set the threshold_qty on variants"""
        templates = super().create(vals_list)

        # For each template, set the threshold on its variants if they exist
        for template in templates:
            if template.threshold_qty and template.product_variant_ids:
                template.product_variant_ids.write({
                    'threshold_qty': template.threshold_qty
                })

        return templates

    def write(self, vals):
        """When updating template threshold, update variants only if they haven't been customized"""
        result = super().write(vals)

        if 'threshold_qty' in vals:
            for template in self:
                # Only update variants that still have the default value (same as template)
                for variant in template.product_variant_ids:
                    # You might want to add a flag to track if variant threshold was manually set
                    # For now, we'll update all variants (you can modify this logic)
                    variant.threshold_qty = vals['threshold_qty']

        return result

    # Add field to template level
    # store=True to mirror product.product and recompute eagerly whenever the
    # underlying variant's manufacturable_qty changes.
    manufacturable_qty = fields.Float(
        string='Manufacturable Quantity',
        compute='_compute_manufacturable_qty',
        store=True,
        help='Maximum number of units that can be manufactured based on available component stock'
    )

    manufacturable_qty_uom = fields.Char(
        string='Manufacturable UOM',
        compute='_compute_manufacturable_qty',
        store=True,
        help='Unit of measure for manufacturable quantity'
    )

    is_finished_product = fields.Boolean(string='Is Finished Product')

    @api.depends('product_variant_ids.manufacturable_qty', 'product_variant_ids.manufacturable_qty_uom')
    def _compute_manufacturable_qty(self):
        """Compute manufacturable quantity at template level.

        Relies on product.product.manufacturable_qty already being computed
        and stored - we just read it here rather than calling the private
        compute method directly, so Odoo's own dependency graph (not manual
        calls) is what drives the recompute. This hop is reliable because
        manufacturable_qty is a plain stored field with a normal one2many
        depends path, unlike free_qty.
        """
        for template in self:
            variants = template.product_variant_ids
            if not variants:
                _logger.warning(
                    "%s product.template._compute_manufacturable_qty: %s (id=%s) has no variants -> 0",
                    LOG_TAG, template.display_name, template.id
                )
                template.manufacturable_qty = 0.0
                template.manufacturable_qty_uom = ''
                continue

            # Use the first variant as the template-level representation
            # (matches the single-variant case naturally, and gives a sane
            # default for templates with multiple variants).
            first_variant = variants[0]
            _logger.warning(
                "%s product.template._compute_manufacturable_qty: %s (id=%s) -> using variant %s "
                "(id=%s) manufacturable_qty=%s out of variants %s",
                LOG_TAG, template.display_name, template.id,
                first_variant.display_name, first_variant.id, first_variant.manufacturable_qty,
                variants.mapped(lambda v: (v.display_name, v.manufacturable_qty)),
            )
            template.manufacturable_qty = first_variant.manufacturable_qty
            template.manufacturable_qty_uom = first_variant.manufacturable_qty_uom