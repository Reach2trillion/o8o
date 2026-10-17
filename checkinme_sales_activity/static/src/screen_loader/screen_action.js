/** @odoo-module **/

import { Component, xml } from "@odoo/owl";
import { LazyComponent } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

/**
 * Client action "checkinme_sales_activity.screen": loads the phone screen bundle on demand
 * (JS + CSS of checkinme_sales_activity.assets_screen) and renders the screen component.
 */
export class CheckinmeScreenAction extends Component {
    static template = xml`<LazyComponent bundle="'checkinme_sales_activity.assets_screen'" Component="'checkinme_sales_activity.Screen'" props="props"/>`;
    static components = { LazyComponent };
    static props = { ...standardActionServiceProps };
}

registry.category("actions").add("checkinme_sales_activity.screen", CheckinmeScreenAction);
