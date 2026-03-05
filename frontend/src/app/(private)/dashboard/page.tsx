import CalendarCard from '@/components/dashboard/CalendarCard';
import RemindersCard from '@/components/dashboard/RemindersCard';
import FocusTimerCard from '@/components/dashboard/FocusTimerCard';
import SystemHealthCard from '@/components/dashboard/SystemHealthCard';
import FinanceSnapshotCard from '@/components/dashboard/FinanceSnapshotCard';
import TemperatureCard from '@/components/dashboard/TemperatureCard';

export default function DashboardPage() {
  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Today</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        <CalendarCard />
        <RemindersCard />
        <FocusTimerCard />
        <SystemHealthCard />
        <TemperatureCard />
        <FinanceSnapshotCard />
      </div>
    </div>
  );
}
