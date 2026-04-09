import { useRef, useCallback, useState } from 'react';

/**
 * Queue for turbo-mode fast scanning.
 * Enqueues barcodes and processes them sequentially so rapid scans
 * don't race or get dropped while an API call is in-flight.
 *
 * Pass errorRef (from useScreenError) so the queue pauses while an
 * error popup is visible — prevents stacked popups and crashes.
 *
 * Returns [enqueue, isProcessing]
 */
export default function useScanQueue(processFn, errorRef) {
  const queue = useRef([]);
  const processingRef = useRef(false);
  const [isProcessing, setIsProcessing] = useState(false);

  const processQueue = useCallback(async () => {
    if (processingRef.current) return;
    processingRef.current = true;
    setIsProcessing(true);

    while (queue.current.length > 0) {
      // Pause while an error popup is visible
      if (errorRef?.current) {
        await new Promise((resolve) => {
          const check = setInterval(() => {
            if (!errorRef.current) {
              clearInterval(check);
              resolve();
            }
          }, 100);
        });
      }

      const barcode = queue.current.shift();
      console.log('[SCAN_DEBUG] useScanQueue processing:', JSON.stringify(barcode), 'remaining:', queue.current.length);
      try {
        await processFn(barcode);
      } catch {
        // errors handled inside processFn
      }
    }

    processingRef.current = false;
    setIsProcessing(false);
  }, [processFn, errorRef]);

  const enqueue = useCallback((barcode) => {
    queue.current.push(barcode);
    processQueue();
  }, [processQueue]);

  return [enqueue, isProcessing];
}
