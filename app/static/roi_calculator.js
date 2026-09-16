(function () {
  var tc = document.getElementById("calc-tc");
  var min = document.getElementById("calc-min");
  var cycles = document.getElementById("calc-cycles");
  var rate = document.getElementById("calc-rate");
  var country = document.getElementById("calc-country");
  if (!tc || !min || !cycles || !rate) return; // section not on this page

  // Suggested hourly rate per country, in AUD - a general industry-benchmark
  // starting point, not a precise figure. Derived from published median
  // salary/contractor-rate benchmarks per country (checked 2026-09-16),
  // converted to AUD at that day's approximate exchange rate. Both the
  // underlying benchmark and the exchange rate drift over time, so this is
  // meant only as an editable starting point for the visitor, never a fixed
  // truth - the field stays editable regardless of country chosen.
  var COUNTRY_RATES_AUD = {
    AU: 45,
    US: 49,
    UK: 65,
    CA: 78,
    NZ: 38,
    IE: 58,
    SG: 116
  };

  if (country) {
    country.addEventListener("change", function () {
      var suggested = COUNTRY_RATES_AUD[country.value];
      if (suggested) {
        rate.value = suggested;
        recalc();
      }
    });
  }

  var outHours = document.getElementById("calc-out-hours");
  var outMonth = document.getElementById("calc-out-month");
  var outYear = document.getElementById("calc-out-year");
  var cta = document.getElementById("calc-cta");

  function fmtMoney(n) {
    return "$" + Math.round(n).toLocaleString("en-AU");
  }
  function fmtHours(n) {
    // One decimal only when it's not a whole number, so "40" doesn't show
    // as "40.0" but "6.5" still shows its half-hour.
    var rounded = Math.round(n * 10) / 10;
    return (rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)) + " hrs";
  }

  function clampPositive(v, fallback) {
    var n = parseFloat(v);
    return isFinite(n) && n > 0 ? n : fallback;
  }

  function recalc() {
    var tcVal = clampPositive(tc.value, 0);
    var minVal = clampPositive(min.value, 0);
    var cyclesVal = clampPositive(cycles.value, 0);
    var rateVal = clampPositive(rate.value, 0);

    var hoursPerMonth = (tcVal * minVal * cyclesVal) / 60;
    var costPerMonth = hoursPerMonth * rateVal;
    var costPerYear = costPerMonth * 12;

    outHours.textContent = fmtHours(hoursPerMonth);
    outMonth.textContent = fmtMoney(costPerMonth);
    outYear.textContent = fmtMoney(costPerYear);

    if (cta) {
      var body = "Company: \nWhat you are testing (platform/app): \n\n" +
        "For reference, based on the calculator on your site:\n" +
        "- " + tcVal + " test cases per cycle\n" +
        "- " + cyclesVal + " cycles/month\n" +
        "- currently about " + fmtHours(hoursPerMonth) + "/month and " + fmtMoney(costPerMonth) + "/month manually\n";
      cta.href = "mailto:kalyan@req2qa.com?subject=" + encodeURIComponent("Access request - Req2QA") +
        "&body=" + encodeURIComponent(body);
    }
  }

  [tc, min, cycles, rate].forEach(function (el) {
    el.addEventListener("input", recalc);
  });
  recalc();
})();
