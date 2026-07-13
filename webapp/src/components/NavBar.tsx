import { NavLink } from "react-router-dom";
import { IconCalendar, IconChat, IconPulse, IconTarget } from "./icons";

const links = [
  { to: "/", label: "Today", icon: IconPulse },
  { to: "/chat", label: "Chat", icon: IconChat },
  { to: "/goals", label: "Goals", icon: IconTarget },
  { to: "/plan", label: "Plan", icon: IconCalendar },
];

export default function NavBar() {
  return (
    <nav className="bottom-nav">
      <div className="bottom-nav-inner">
        {links.map((l) => {
          const Icon = l.icon;
          return (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.to === "/"}
              className={({ isActive }) => "bottom-nav-link" + (isActive ? " active" : "")}
            >
              <Icon />
              {l.label}
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
