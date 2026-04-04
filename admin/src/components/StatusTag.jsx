const STATUS_MAP = {
  OPEN: 'tag-info',
  'Ready to pick': 'tag-info',
  PARTIAL: 'tag-warning',
  PICKING: 'tag-purple',
  ALLOCATED: 'tag-purple',
  IN_PROGRESS: 'tag-purple',
  PACKED: 'tag-success',
  COMPLETED: 'tag-success',
  COMPLETE: 'tag-success',
  RECEIVED: 'tag-success',
  SHIPPED: 'tag-gray',
  CLOSED: 'tag-gray',
  CANCELLED: 'tag-gray',
  INACTIVE: 'tag-gray',
  LOW: 'tag-danger',
  VARIANCE: 'tag-danger',
  SHORT: 'tag-danger',
};

export default function StatusTag({ status }) {
  if (!status) return null;
  const cls = STATUS_MAP[status] || 'tag-gray';
  return <span className={`tag ${cls}`}>{status}</span>;
}
