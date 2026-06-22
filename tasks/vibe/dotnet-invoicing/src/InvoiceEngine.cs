namespace Invoicing;

/// <summary>
/// Computes an itemized invoice total: subtotal, discounts, taxes, grand total.
///
/// Ordering contract (see the team's pricing spec):
///   1. subtotal   = sum of per-line totals (UnitPrice * Quantity)
///   2. discounts  = order-level percent + flat amount, applied to the subtotal
///                   BEFORE tax; the discounted base never goes below zero
///   3. taxes      = applied in order to the discounted base; a compound tax
///                   stacks on the base plus taxes already applied
///   4. grandTotal = discounted base + total tax, never negative
/// All money is rounded to 2 decimal places, away from zero.
/// </summary>
public static class InvoiceEngine
{
    private static decimal Round(decimal d) =>
        Math.Round(d, 2, MidpointRounding.AwayFromZero);

    public static InvoiceResult Compute(InvoiceRequest req)
    {
        // Subtotal: total all the lines, then round to cents.
        decimal subtotal = 0m;
        foreach (var item in req.Items)
        {
            subtotal += item.UnitPrice * item.Quantity;
        }
        subtotal = Round(subtotal);

        // Discounts: order-level percent plus any flat amount off.
        decimal percentDiscount = Round(subtotal * req.DiscountPercent / 100m);
        decimal discount = percentDiscount + req.FixedDiscount;

        // Taxes: apply each configured rate to the subtotal.
        decimal taxTotal = 0m;
        foreach (var rate in req.Taxes)
        {
            decimal taxable = subtotal;
            taxTotal += Round(taxable * rate.Percent / 100m);
        }

        // Grand total: subtotal plus tax, less the discount.
        decimal grandTotal = Round(subtotal + taxTotal - discount);

        return new InvoiceResult(
            Subtotal: subtotal,
            DiscountTotal: Round(discount),
            Tax: Round(taxTotal),
            GrandTotal: grandTotal);
    }
}
