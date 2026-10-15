const paths = {
  refresh: ["M3 11a9 9 0 0 1 15.4-6.4L21 7", "M21 3v4h-4", "M21 13a9 9 0 0 1-15.4 6.4L3 17", "M7 17H3v4"],
  back: ["m12 19-7-7 7-7", "M5 12h14"],
  chevron: ["m9 18 6-6-6-6"],
  users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M16 3a4 4 0 0 1 0 8", "M22 21v-2a4 4 0 0 0-3-3.87", "M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0"],
  plus: ["M12 5v14", "M5 12h14"],
  external: ["M15 3h6v6", "M10 14 21 3", "M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"],
  trash: ["M3 6h18", "M19 6v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6", "M8 6V3h8v3", "M10 10v7", "M14 10v7"],
} satisfies Record<string, string[]>;

export type IconName = keyof typeof paths;

/** Small, decorative line icons; the owning control supplies its translated name. */
export function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", width: "16", height: "16", fill: "none",
    stroke: "currentColor", "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: "corp-icon" })) svg.setAttribute(key, value);
  for (const d of paths[name]) {
    const path = document.createElementNS(svg.namespaceURI, "path"); path.setAttribute("d", d); svg.append(path);
  }
  return svg;
}
