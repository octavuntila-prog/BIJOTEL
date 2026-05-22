// Pagination — load-more pattern (no page numbers).
//
// Props:
//   loaded:    number of entries shown so far
//   total:     total available
//   onLoadMore: () => void  (called when the user clicks "Load more")
//   loading:   bool — disable the button while a request is in flight

export default function Pagination({ loaded, total, onLoadMore, loading }) {
  const hasMore = loaded < total
  return (
    <div className="flex items-center justify-between border-t border-gray-200 pt-3 mt-2 text-sm text-gray-600">
      <div>
        Showing <span className="font-medium text-gray-900">{loaded}</span> of{' '}
        <span className="font-medium text-gray-900">{total.toLocaleString()}</span>{' '}
        entries
      </div>
      {hasMore && (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loading}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
