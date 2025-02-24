import graphene
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from ....channel import models
from ....channel.error_codes import ChannelErrorCode
from ....core.tracing import traced_atomic_transaction
from ....permission.enums import ChannelPermissions
from ....tax.models import TaxConfiguration
from ...account.enums import CountryCodeEnum
from ...core import ResolveInfo
from ...core.descriptions import (
    ADDED_IN_31,
    ADDED_IN_35,
    ADDED_IN_37,
    ADDED_IN_312,
    ADDED_IN_313,
    PREVIEW_FEATURE,
)
from ...core.doc_category import (
    DOC_CATEGORY_CHANNELS,
    DOC_CATEGORY_ORDERS,
    DOC_CATEGORY_PRODUCTS,
)
from ...core.mutations import ModelMutation
from ...core.scalars import Minute
from ...core.types import BaseInputObjectType, ChannelError, NonNullList
from ...plugins.dataloaders import get_plugin_manager_promise
from ..enums import (
    AllocationStrategyEnum,
    MarkAsPaidStrategyEnum,
    TransactionFlowStrategyEnum,
)
from ..types import Channel


class StockSettingsInput(BaseInputObjectType):
    allocation_strategy = AllocationStrategyEnum(
        description=(
            "Allocation strategy options. Strategy defines the preference "
            "of warehouses for allocations and reservations."
        ),
        required=True,
    )

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class OrderSettingsInput(BaseInputObjectType):
    automatically_confirm_all_new_orders = graphene.Boolean(
        required=False,
        description="When disabled, all new orders from checkout "
        "will be marked as unconfirmed. When enabled orders from checkout will "
        "become unfulfilled immediately. By default set to True",
    )
    automatically_fulfill_non_shippable_gift_card = graphene.Boolean(
        required=False,
        description="When enabled, all non-shippable gift card orders "
        "will be fulfilled automatically. By defualt set to True.",
    )
    expire_orders_after = Minute(
        required=False,
        description=(
            "Expiration time in minutes. "
            "Default null - means do not expire any orders. "
            "Enter 0 or null to disable." + ADDED_IN_313 + PREVIEW_FEATURE
        ),
    )
    mark_as_paid_strategy = MarkAsPaidStrategyEnum(
        required=False,
        description=(
            "Determine what strategy will be used to mark the order as paid. "
            "Based on the chosen option, the proper object will be created "
            "and attached to the order when it's manually marked as paid."
            "\n`PAYMENT_FLOW` - [default option] creates the `Payment` object."
            "\n`TRANSACTION_FLOW` - creates the `TransactionItem` object."
            + ADDED_IN_313
            + PREVIEW_FEATURE
        ),
    )
    default_transaction_flow_strategy = TransactionFlowStrategyEnum(
        required=False,
        description=(
            "Determine the transaction flow strategy to be used. "
            "Include the selected option in the payload sent to the payment app, as a "
            "requested action for the transaction." + ADDED_IN_313 + PREVIEW_FEATURE
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_ORDERS


class ChannelInput(BaseInputObjectType):
    is_active = graphene.Boolean(description="isActive flag.")
    stock_settings = graphene.Field(
        StockSettingsInput,
        description=("The channel stock settings." + ADDED_IN_37 + PREVIEW_FEATURE),
        required=False,
    )
    add_shipping_zones = NonNullList(
        graphene.ID,
        description="List of shipping zones to assign to the channel.",
        required=False,
    )
    add_warehouses = NonNullList(
        graphene.ID,
        description="List of warehouses to assign to the channel."
        + ADDED_IN_35
        + PREVIEW_FEATURE,
        required=False,
    )
    order_settings = graphene.Field(
        OrderSettingsInput,
        description="The channel order settings" + ADDED_IN_312,
        required=False,
    )

    class Meta:
        doc_category = DOC_CATEGORY_CHANNELS


class ChannelCreateInput(ChannelInput):
    name = graphene.String(description="Name of the channel.", required=True)
    slug = graphene.String(description="Slug of the channel.", required=True)
    currency_code = graphene.String(
        description="Currency of the channel.", required=True
    )
    default_country = CountryCodeEnum(
        description=(
            "Default country for the channel. Default country can be "
            "used in checkout to determine the stock quantities or calculate taxes "
            "when the country was not explicitly provided."
            + ADDED_IN_31
            + PREVIEW_FEATURE
        ),
        required=True,
    )

    class Meta:
        doc_category = DOC_CATEGORY_CHANNELS


class ChannelCreate(ModelMutation):
    class Arguments:
        input = ChannelCreateInput(
            required=True, description="Fields required to create channel."
        )

    class Meta:
        description = "Creates new channel."
        model = models.Channel
        object_type = Channel
        permissions = (ChannelPermissions.MANAGE_CHANNELS,)
        error_type_class = ChannelError
        error_type_field = "channel_errors"

    @classmethod
    def get_type_for_model(cls):
        return Channel

    @classmethod
    def clean_input(cls, info: ResolveInfo, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        slug = cleaned_input.get("slug")
        if slug:
            cleaned_input["slug"] = slugify(slug)
        if stock_settings := cleaned_input.get("stock_settings"):
            cleaned_input["allocation_strategy"] = stock_settings["allocation_strategy"]
        if order_settings := cleaned_input.get("order_settings"):
            automatically_confirm_all_new_orders = order_settings.get(
                "automatically_confirm_all_new_orders"
            )
            if automatically_confirm_all_new_orders is not None:
                cleaned_input[
                    "automatically_confirm_all_new_orders"
                ] = automatically_confirm_all_new_orders

            automatically_fulfill_non_shippable_gift_card = order_settings.get(
                "automatically_fulfill_non_shippable_gift_card"
            )
            if automatically_fulfill_non_shippable_gift_card is not None:
                cleaned_input[
                    "automatically_fulfill_non_shippable_gift_card"
                ] = automatically_fulfill_non_shippable_gift_card
            if mark_as_paid_strategy := order_settings.get("mark_as_paid_strategy"):
                cleaned_input["order_mark_as_paid_strategy"] = mark_as_paid_strategy

            if "expire_orders_after" in order_settings:
                expire_orders_after = order_settings["expire_orders_after"]
                cleaned_input["expire_orders_after"] = cls.clean_expire_orders_after(
                    expire_orders_after
                )
            if default_transaction_strategy := order_settings.get(
                "default_transaction_flow_strategy"
            ):
                cleaned_input[
                    "default_transaction_flow_strategy"
                ] = default_transaction_strategy

        return cleaned_input

    @classmethod
    def clean_expire_orders_after(cls, expire_orders_after):
        if expire_orders_after is None or expire_orders_after == 0:
            return None
        if expire_orders_after < 0:
            raise ValidationError(
                {
                    "expire_orders_after": ValidationError(
                        "Expiration time for orders cannot be lower than 0.",
                        code=ChannelErrorCode.INVALID.value,
                    )
                }
            )
        return expire_orders_after

    @classmethod
    def _save_m2m(cls, info: ResolveInfo, instance, cleaned_data):
        with traced_atomic_transaction():
            super()._save_m2m(info, instance, cleaned_data)
            shipping_zones = cleaned_data.get("add_shipping_zones")
            if shipping_zones:
                instance.shipping_zones.add(*shipping_zones)
            warehouses = cleaned_data.get("add_warehouses")
            if warehouses:
                instance.warehouses.add(*warehouses)

    @classmethod
    def post_save_action(cls, info: ResolveInfo, instance, cleaned_input):
        TaxConfiguration.objects.create(channel=instance)
        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.channel_created, instance)
