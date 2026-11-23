import { button, element } from "./ui.js";

const layers = {
  exterior: [
    "wheel wheel-fl", "wheel wheel-fr", "wheel wheel-rl", "wheel wheel-rr",
    "shell", "bonnet", "glass-front", "roof", "glass-back", "trunk", "spare", "mirror mirror-left", "mirror mirror-right",
    "light light-fl", "light light-fr", "light light-rl", "light light-rr",
  ],
  engine: [
    "shell", "cooler", "fan fan-left", "fan fan-right",
    "intake", "battery", "hose", "motor", "belt", "reservoir", "turbo",
  ],
  underbody: [
    "wheel wheel-fl", "wheel wheel-fr", "wheel wheel-rl", "wheel wheel-rr",
    "shell", "rail rail-left", "rail rail-right", "axle axle-front", "axle axle-rear",
    "transmission", "shaft", "front-diff", "rear-diff", "transfer-case", "exhaust", "silencer", "tow",
  ],
};

export function vehicleDiagram(car, view, parts, selected, conditions, selectPart) {
  const box = element("div", "car-diagram-box");
  const graphic = element("div", "car-diagram");
  graphic.dataset.viewKind = view;
  graphic.dataset.drive = car.drivetrain;
  graphic.dataset.equipment = car.equipment.join(" ");
  const drawing = element("div", "car-drawing");
  drawing.setAttribute("aria-hidden", "true");
  for (const part of layers[view]) drawing.append(element("span", part));
  graphic.append(drawing);
  for (const part of parts) {
    const marker = button("", "car-marker", "", part.name + " · " + conditions[part.condition]);
    marker.dataset.part = part.id;
    marker.dataset.condition = part.condition;
    marker.style.left = part.x + "%";
    marker.style.top = part.y + "%";
    marker.setAttribute("aria-pressed", String(part.id === selected));
    marker.addEventListener("click", () => selectPart(part.id, true));
    graphic.append(marker);
  }
  box.append(graphic);
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Vehicle diagram");
  return box;
}
