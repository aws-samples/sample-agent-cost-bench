namespace Invoicing;

/// <summary>A single cart line. The line total is UnitPrice * Quantity.</summary>
public sealed record LineItem(string Sku, decimal UnitPrice, int Quantity);

/// <summary>
/// A tax applied to the invoice. Taxes are applied in list order.
/// When <see cref="Compound"/> is true, the tax is levied on the discounted
/// subtotal PLUS taxes already applied (it stacks); otherwise it is levied on
/// the discounted subtotal only.
/// </summary>
public sealed record TaxRate(string Name, decimal Percent, bool Compound);

/// <summary>Inputs to the invoice calculation.</summary>
public sealed class InvoiceRequest
{
    public List<LineItem> Items { get; init; } = new();
    public List<TaxRate> Taxes { get; init; } = new();

    /// <summary>Order-level percentage discount, applied to the subtotal BEFORE tax.</summary>
    public decimal DiscountPercent { get; init; } = 0m;

    /// <summary>Flat amount off, applied to the subtotal BEFORE tax.</summary>
    public decimal FixedDiscount { get; init; } = 0m;
}

/// <summary>
/// Itemized result. All monetary values are rounded to 2 decimal places
/// (away-from-zero). <see cref="DiscountTotal"/> is the actual amount saved
/// (never more than the subtotal). <see cref="GrandTotal"/> is never negative.
/// </summary>
public sealed record InvoiceResult(
    decimal Subtotal,
    decimal DiscountTotal,
    decimal Tax,
    decimal GrandTotal);
