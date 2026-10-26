// The one navigation list — the desktop sidebar shows all of it in this order;
// the mobile tab bar shows the `primary` items and puts the rest in "More".
// Feature-gated entries are filtered by isNavItemEnabled() at render time.

import {
  CalendarDays,
  Coins,
  Dumbbell,
  FileText,
  Home,
  LayoutDashboard,
  ListTodo,
  MessageSquare,
  Network,
  Pill,
  Refrigerator,
  Settings,
  ShoppingCart,
  UtensilsCrossed,
  Volume2,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Shown as a mobile bottom tab (in this order) instead of under More. */
  primary?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard, primary: true },
  { href: '/chat', label: 'Chat', icon: MessageSquare, primary: true },
  { href: '/tasks', label: 'Tasks', icon: ListTodo, primary: true },
  { href: '/calendar', label: 'Calendar', icon: CalendarDays },
  { href: '/meals', label: 'Meals', icon: UtensilsCrossed, primary: true },
  { href: '/workouts', label: 'Workouts', icon: Dumbbell, primary: true },
  { href: '/pantry', label: 'Pantry', icon: Refrigerator },
  { href: '/shopping', label: 'Shopping', icon: ShoppingCart },
  { href: '/documents', label: 'Documents', icon: FileText },
  { href: '/finance', label: 'Finance', icon: Coins },
  { href: '/home', label: 'Home', icon: Home },
  { href: '/announcements', label: 'Announcements', icon: Volume2 },
  { href: '/architecture', label: 'Architecture', icon: Network },
  { href: '/personal-facts', label: 'Facts', icon: Pill },
  { href: '/settings', label: 'Settings', icon: Settings },
];

export function isNavActive(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  return pathname === href || pathname.startsWith(href + '/');
}
