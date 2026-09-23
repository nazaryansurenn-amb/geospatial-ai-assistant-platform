import React from "react";
const fmt = new Intl.NumberFormat("hy-AM", {maximumFractionDigits: 2});
const countFmt = new Intl.NumberFormat("hy-AM");
export const formatCountArea = (count, area) => `${countFmt.format(count ?? 0)} · ${Number.isFinite(area) ? fmt.format(area) : "—"} հա`;
export default function AreaMetric({count, area}) {
  return <span className="parcel-count-area"><span>{countFmt.format(count ?? 0)}</span><small title="Կադաստրային մակերես">{Number.isFinite(area) ? fmt.format(area) : "—"} հա</small></span>;
}
