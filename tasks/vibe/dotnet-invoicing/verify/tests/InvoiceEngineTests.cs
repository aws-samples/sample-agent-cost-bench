using Invoicing;
using Xunit;

namespace Invoicing.Tests;

public class InvoiceEngineTests
{
    private static LineItem Item(decimal price, int qty, string sku = "SKU") => new(sku, price, qty);

    [Fact]
    public void Subtotal_no_discount_no_tax()
    {
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(10.00m, 3), Item(5.50m, 2) }, // 30 + 11 = 41
        });
        Assert.Equal(41.00m, r.Subtotal);
        Assert.Equal(0.00m, r.Tax);
        Assert.Equal(41.00m, r.GrandTotal);
        Assert.Equal(0.00m, r.DiscountTotal);
    }

    [Fact]
    public void Single_tax_no_discount()
    {
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(100.00m, 1) },
            Taxes = { new TaxRate("VAT", 10m, Compound: false) },
        });
        Assert.Equal(100.00m, r.Subtotal);
        Assert.Equal(10.00m, r.Tax);
        Assert.Equal(110.00m, r.GrandTotal);
    }

    [Fact]
    public void Percent_discount_is_applied_before_tax()
    {
        // 10% off 100 -> base 90, tax 10% of 90 = 9, grand 99.
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(100.00m, 1) },
            DiscountPercent = 10m,
            Taxes = { new TaxRate("VAT", 10m, Compound: false) },
        });
        Assert.Equal(10.00m, r.DiscountTotal);
        Assert.Equal(9.00m, r.Tax);
        Assert.Equal(99.00m, r.GrandTotal);
    }

    [Fact]
    public void Compound_tax_stacks_on_prior_tax()
    {
        // base 100; VAT 10% = 10; LEVY 10% compound on 110 = 11; tax 21; grand 121.
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(100.00m, 1) },
            Taxes =
            {
                new TaxRate("VAT", 10m, Compound: false),
                new TaxRate("LEVY", 10m, Compound: true),
            },
        });
        Assert.Equal(21.00m, r.Tax);
        Assert.Equal(121.00m, r.GrandTotal);
    }

    [Fact]
    public void Fixed_discount_larger_than_subtotal_clamps_to_zero()
    {
        // subtotal 30, fixed discount 50 -> base clamps to 0, actual saved 30.
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(30.00m, 1) },
            FixedDiscount = 50m,
        });
        Assert.Equal(30.00m, r.Subtotal);
        Assert.Equal(30.00m, r.DiscountTotal);
        Assert.Equal(0.00m, r.Tax);
        Assert.Equal(0.00m, r.GrandTotal);
        Assert.True(r.GrandTotal >= 0m);
    }

    [Fact]
    public void Per_line_rounding_applied_to_each_line()
    {
        // Two lines of 0.125 each: per-line round (away from zero) = 0.13 each
        // -> subtotal 0.26. Rounding the raw sum (0.25) at the end is wrong.
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(0.125m, 1, "A"), Item(0.125m, 1, "B") },
        });
        Assert.Equal(0.26m, r.Subtotal);
        Assert.Equal(0.26m, r.GrandTotal);
    }

    [Fact]
    public void Combined_percent_and_fixed_then_compound_tax()
    {
        // subtotal 200; 10% -> 20; fixed 30; discount 50; base 150.
        // VAT 5% = 7.50; LEVY 5% compound on 157.50 = 7.88 (7.875 -> away 7.88).
        // tax 15.38; grand 165.38.
        var r = InvoiceEngine.Compute(new InvoiceRequest
        {
            Items = { Item(50.00m, 4) },
            DiscountPercent = 10m,
            FixedDiscount = 30m,
            Taxes =
            {
                new TaxRate("VAT", 5m, Compound: false),
                new TaxRate("LEVY", 5m, Compound: true),
            },
        });
        Assert.Equal(50.00m, r.DiscountTotal);
        Assert.Equal(15.38m, r.Tax);
        Assert.Equal(165.38m, r.GrandTotal);
    }
}
