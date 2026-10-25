;;; SBCL-only versioned session. Only its owner thread touches the socket.

(in-package #:lisp-mcp)

(defconstant +genera-control-limit+ 16)
(defconstant +genera-history-limit+ 32)

(define-condition genera-session-error (simple-error)
  ((kind :initarg :kind :reader genera-session-error-kind)
   (session :initarg :session :initform nil :reader genera-session-error-session)
   (response :initarg :response :initform nil :reader genera-session-error-response)))

(defun genera-session-error (kind message &optional response)
  (error 'genera-session-error :kind kind :response response
         :format-control "~A" :format-arguments (list message)))

(defstruct (genera-request (:constructor %make-genera-request))
  envelope payload execution-p deadline (state :queued) response failure
  remote-state acknowledged delivered)

(defstruct (genera-session (:constructor %make-genera-session))
  host port token expected-world (io-timeout 5) owner socket hello
  (lock (sb-thread:make-mutex :name "Genera session"))
  (changed (sb-thread:make-waitqueue :name "Genera session"))
  (nonce (format nil "~A-~D" *genera-request-nonce*
                 (random (expt 2 64) (make-random-state t))))
  (counter 0) (state :connecting) (phase :connect) blocking stop failure cached-status
  (pending (make-hash-table :test 'equal)) queue execution (controls 0) history
  (history-evicted 0)
  ;; Incremental framing lets queued controls pass even during a partial reply.
  (prefix :start) (digits 0) (length 0) input (position 0))

(defun genera-session-checked (function)
  (handler-case (funcall function)
    (error (condition) (genera-session-error :protocol (princ-to-string condition)))))

(defmacro genera-with-io (session &body body)
  `(unwind-protect
       (progn
         (setf (genera-session-blocking ,session) t)
         (when (genera-session-stop ,session)
           (genera-session-error :closed "Genera host I/O closed"))
         ,@body)
     (setf (genera-session-blocking ,session) nil)))

(defun genera-now () (/ (get-internal-real-time) internal-time-units-per-second))

(defun genera-remember (session request)
  ;; Called with the session lock. Unknown acknowledgements remain unknown.
  (pushnew request (genera-session-history session) :test #'eq)
  (when (> (length (genera-session-history session)) +genera-history-limit+)
    (let ((oldest (find-if (lambda (item)
                            (or (not (eq (genera-request-state item) :unresolved))
                                (genera-request-acknowledged item)))
                          (reverse (genera-session-history session)))))
      (unless oldest (genera-session-error :busy "Unresolved history capacity is occupied"))
      (setf (genera-session-history session) (delete oldest (genera-session-history session)))
      (incf (genera-session-history-evicted session)))))

(defun genera-release (session request)
  (remhash (getf (genera-request-envelope request) :id) (genera-session-pending session))
  (if (genera-request-execution-p request)
      (when (eq request (genera-session-execution session))
        (genera-remember session request)
        (setf (genera-session-execution session)
              (find-if (lambda (item)
                         (and (eq (genera-request-state item) :unresolved)
                              (not (genera-request-acknowledged item))))
                       (genera-session-history session))))
      (decf (genera-session-controls session))))

(defun genera-expire-request (session request)
  (when (and (genera-request-deadline request)
             (>= (genera-now) (genera-request-deadline request))
             (null (genera-request-response request)) (null (genera-request-failure request)))
    (setf (genera-request-failure request)
          (make-condition 'genera-session-error :kind :timeout :session session
                          :format-control "Host wait deadline expired; Genera work was not stopped"))
    (if (eq (genera-request-state request) :queued)
        (progn
          (setf (genera-request-state request) :not-submitted
                (genera-session-queue session) (delete request (genera-session-queue session)))
          (genera-release session request))
        (setf (genera-request-state request) :unresolved))
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-session-fail (session condition)
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (slot-value condition 'session) session)
    (setf (genera-session-failure session) condition
          (genera-session-state session) (if (genera-session-stop session) :closed :failed)
          (genera-session-phase session) :failed
          (genera-session-queue session) nil)
    (dolist (request (loop for value being the hash-values of (genera-session-pending session)
                          collect value))
      (unless (genera-request-failure request) (setf (genera-request-failure request) condition))
      (if (eq (genera-request-state request) :queued)
          (progn (setf (genera-request-state request) :not-submitted)
                 (genera-release session request))
          (setf (genera-request-state request) :unresolved)))
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-session-check-response (request response)
  (unless (every (lambda (key) (equal (getf request key) (getf response key)))
                 '(:id :boot-id :service-id))
    (mcp-v1-error "Response identity does not match its request"))
  (case (getf request :op)
    (:eval (mcp-v1-check-eval-response request response))
    (:status
     (when (getf response :ok)
       (dolist (key '(:world :package :busy :features))
         (unless (mcp-v1-present-p response key) (mcp-v1-error "Incomplete status")))))
    (:request-status
     (when (getf response :ok)
       (unless (and (equal (getf request :target-id) (getf response :target-id))
                    (mcp-v1-present-p response :request-state)
                    (eq (not (null (getf response :result)))
                        (not (null (member (getf response :request-state)
                                           '(:completed :failed :interrupted))))))
         (mcp-v1-error "Incomplete or mismatched request status")))))
  response)

(defun genera-session-deliver (session text)
  (let ((response (genera-session-checked (lambda () (mcp-v1-decode text :response)))))
    (sb-thread:with-mutex ((genera-session-lock session))
      (let* ((id (getf response :id))
             (pending (gethash id (genera-session-pending session)))
             (request (or pending
                          (find id (genera-session-history session)
                                :key (lambda (item) (getf (genera-request-envelope item) :id))
                                :test #'equal))))
        (unless (and request (not (genera-request-delivered request))
                     (member (genera-request-state request) '(:sent :unresolved :unknown :completed)))
          (genera-session-error :protocol "Unmatched or repeated response ID"))
        (genera-session-checked
         (lambda () (genera-session-check-response (genera-request-envelope request) response)))
        (when (and (getf response :ok)
                   (eq (getf (genera-request-envelope request) :op) :status))
          (unless (equal (getf response :world) (getf (genera-session-hello session) :world))
            (genera-session-error :protocol "Status world changed without a new generation"))
          (setf (genera-session-cached-status session) response))
        ;; Validate everything before completing a waiter or releasing its slot.
        (when (and (genera-request-response request)
                   (not (equal response (genera-request-response request))))
          (genera-session-error :protocol "Late result conflicts with reconciled result"))
        (setf (genera-request-response request) response
              (genera-request-state request)
              (if (and (genera-request-execution-p request) (not (getf response :request-state)))
                  :not-submitted :completed)
              (genera-request-remote-state request) (getf response :request-state)
              (genera-request-delivered request) t)
        (when (or pending (eq request (genera-session-execution session)))
          (genera-release session request))
        (sb-thread:condition-broadcast (genera-session-changed session))))))

(defun genera-session-byte (session byte)
  (when (genera-session-input session)
    (setf (aref (genera-session-input session) (genera-session-position session)) byte)
    (when (= (incf (genera-session-position session)) (genera-session-length session))
      (genera-session-deliver session (map 'string #'code-char (genera-session-input session)))
      (setf (genera-session-input session) nil (genera-session-prefix session) :start
            (genera-session-digits session) 0 (genera-session-length session) 0
            (genera-session-position session) 0))
    (return-from genera-session-byte nil))
  (case (genera-session-prefix session)
    (:start
     (setf (genera-session-prefix session) :digits)
     (cond ((member byte '(10 141)) (return-from genera-session-byte nil))
           ((= byte 13) (setf (genera-session-prefix session) :after-cr)
                        (return-from genera-session-byte nil))))
    (:after-cr
     (setf (genera-session-prefix session) :digits)
     (when (= byte 10) (return-from genera-session-byte nil))))
  (cond ((<= 48 byte 57)
         (when (> (incf (genera-session-digits session)) +max-header-digits+)
           (framing-error "Length header exceeds digit limit"))
         (setf (genera-session-length session)
               (+ (* 10 (genera-session-length session)) (- byte 48)))
         (when (> (genera-session-length session) +max-frame-length+)
           (framing-error "Response exceeds frame limit")))
        ((and (= byte 32) (plusp (genera-session-digits session)))
         (when (zerop (genera-session-length session))
           (genera-session-error :protocol "Empty response envelope"))
         (setf (genera-session-input session)
               (make-array (genera-session-length session) :element-type '(unsigned-byte 8))))
        (t (framing-error "Expected decimal length followed by a space"))))

(defun genera-session-read-ready (session stream)
  ;; Read at most one bounded batch, yielding to queued writes between batches.
  (loop repeat 8192
        for byte = (read-byte stream nil nil)
        do (unless byte
             (genera-session-error
              (if (eq (genera-session-prefix session) :start) :transport :protocol)
              "Genera closed the connection before pending replies completed"))
           (genera-session-byte session byte)
        while (listen stream)))

(defun genera-session-hello-envelope (session)
  (append (list :version 1 :id (concatenate 'string (genera-session-nonce session) "-hello")
                :op :hello :client "lisp-mcp")
          (when (genera-session-token session) (list :token (genera-session-token session)))
          (when (genera-session-expected-world session)
            (list :expected-world (genera-session-expected-world session)))))

(defun genera-run-session (session)
  (let ((socket nil))
    (handler-case
        (unwind-protect
            (progn
              (genera-with-io session
               (setf socket (usocket:socket-connect
                            (genera-session-host session) (genera-session-port session)
                            :element-type '(unsigned-byte 8)
                            :timeout (genera-session-io-timeout session))))
              (sb-thread:with-mutex ((genera-session-lock session))
                (setf (genera-session-phase session) :hello)
                (when (genera-session-stop session)
                  (genera-session-error :closed "Genera session closed while connecting")))
              (let* ((stream (usocket:socket-stream socket))
                     (hello (genera-session-hello-envelope session))
                     (greeting
                       (genera-with-io session
                        (sb-ext:with-timeout (genera-session-io-timeout session)
                         (genera-write-envelope stream hello)
                         (let* ((size (read-frame-length stream))
                                (data (read-exactly stream
                                        (make-array size :element-type '(unsigned-byte 8))))
                                (text (map 'string #'code-char data)))
                           (genera-session-checked
                            (lambda () (mcp-v1-check-hello-response
                                        hello (mcp-v1-decode text :response)))))))))
                (unless (getf greeting :ok)
                  (genera-session-error :handshake "Genera rejected hello" greeting))
                (sb-thread:with-mutex ((genera-session-lock session))
                  (setf (genera-session-socket session) socket
                        (genera-session-hello session) greeting
                        (genera-session-cached-status session) greeting
                        (genera-session-state session) :ready
                        (genera-session-phase session) :ready)
                  (sb-thread:condition-broadcast (genera-session-changed session)))
                (loop
                  (let ((request
                          (sb-thread:with-mutex ((genera-session-lock session))
                            (when (genera-session-stop session) (return))
                            (dolist (item (copy-list (genera-session-queue session)))
                              (genera-expire-request session item))
                            (let ((next (pop (genera-session-queue session))))
                              (when next (setf (genera-request-state next) :sending))
                              next))))
                    (when request
                      (genera-with-io session
                       (sb-ext:with-timeout (genera-session-io-timeout session)
                        (let ((payload (genera-request-payload request)))
                          (write-sequence (string-to-octets (format nil "~D " (length payload))) stream)
                          (write-sequence payload stream)
                          (force-output stream))))
                      (sb-thread:with-mutex ((genera-session-lock session))
                        (unless (genera-request-failure request)
                          (setf (genera-request-state request) :sent))))
                    (sb-thread:with-mutex ((genera-session-lock session))
                      (dolist (item (loop for value being the hash-values
                                           of (genera-session-pending session) collect value))
                        (genera-expire-request session item)))
                    (when (or (listen stream)
                              (usocket:wait-for-input socket :timeout (if request 0 .02)
                                                            :ready-only t))
                      (genera-session-read-ready session stream))))))
          (when socket
            (ignore-errors (close (usocket:socket-stream socket) :abort t))
            (ignore-errors (usocket:socket-close socket)))
          (sb-thread:with-mutex ((genera-session-lock session))
            (setf (genera-session-socket session) nil)))
      (genera-session-error (condition) (genera-session-fail session condition))
      (framing-error (condition)
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :protocol :format-control "~A"
                          :format-arguments (list (princ-to-string condition)))))
      (sb-ext:timeout ()
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :transport
                          :format-control "Genera socket I/O timed out")))
      (error ()
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :transport
                          :format-control "Genera socket I/O failed"))))
    (unless (genera-session-failure session)
      (genera-session-fail session
        (make-condition 'genera-session-error :kind :closed
                        :format-control "Genera session closed")))))

(defun genera-close-session (session &key interrupt-io)
  "Stop the owner and close its socket; never wait for remote execution."
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (genera-session-stop session) t)
    (sb-thread:condition-broadcast (genera-session-changed session)))
  (when interrupt-io
    (ignore-errors
      (sb-thread:interrupt-thread (genera-session-owner session)
        (lambda ()
          (when (genera-session-blocking session)
            (genera-session-error :closed "Genera host I/O closed"))))))
  (sb-thread:join-thread (genera-session-owner session)
                         :timeout (+ (genera-session-io-timeout session) .5) :default nil)
  (when (sb-thread:thread-alive-p (genera-session-owner session))
    (genera-session-error :shutdown "Genera I/O owner did not stop within its I/O bound"))
  nil)

(defun genera-begin-session (&key (host *genera-host*) (port *genera-port*) token
                                expected-world (io-timeout 5))
  "Start the I/O owner without waiting for connection or hello."
  (unless (and (realp io-timeout) (> io-timeout 0))
    (error "Session I/O timeout must be positive"))
  (let ((session (%make-genera-session :host host :port port :token token
                                     :expected-world expected-world :io-timeout io-timeout)))
    (mcp-v1-encode (genera-session-hello-envelope session) :request)
    (setf (genera-session-owner session)
          (sb-thread:make-thread (lambda () (genera-run-session session)) :name "Genera I/O"))
    session))

(defun genera-open-session (&rest options)
  "Connect and authenticate once, waiting for the asynchronous owner."
  (let ((session (apply #'genera-begin-session options)))
    (let ((ready nil))
      (unwind-protect
          (progn
            (sb-thread:with-mutex ((genera-session-lock session))
              (loop while (eq (genera-session-state session) :connecting)
                    do (sb-thread:condition-wait (genera-session-changed session)
                                                 (genera-session-lock session)))
              (when (genera-session-failure session) (error (genera-session-failure session))))
            (setf ready t)
            session)
        (unless ready (genera-close-session session))))))

(defun genera-submit-request (session operation &rest fields)
  "Reserve one execution or bounded control slot; return a request handle."
  (sb-thread:with-mutex ((genera-session-lock session))
    (unless (and (eq (genera-session-state session) :ready) (not (genera-session-stop session)))
      (error (or (genera-session-failure session)
                 (make-condition 'genera-session-error :kind :closed
                                 :format-control "Genera session is closed"))))
    (unless (member operation '(:eval :status :request-status))
      (genera-session-error :unsupported "Unsupported session operation"))
    (unless (member operation (getf (genera-session-hello session) :features))
      (genera-session-error :unsupported "Listener does not advertise this operation"))
    (let* ((execution (eq operation :eval))
           (timeout (or (getf fields :host-timeout) (if execution *genera-timeout*
                                                       (genera-session-io-timeout session))))
           (hello (genera-session-hello session))
           (ignored (remf fields :host-timeout))
           (envelope (append (list :version 1
                                   :id (format nil "~A-~D" (genera-session-nonce session)
                                               (incf (genera-session-counter session)))
                                   :op operation :boot-id (getf hello :boot-id)
                                   :service-id (getf hello :service-id)) fields))
           (payload (string-to-octets (mcp-v1-encode envelope :request))))
      (declare (ignore ignored))
      (unless (and (realp timeout) (> timeout 0)) (error "Host timeout must be positive"))
      (when (if execution (genera-session-execution session)
                (>= (genera-session-controls session) +genera-control-limit+))
        (genera-session-error :busy "Session request capacity is occupied"))
      (let ((request (%make-genera-request :envelope envelope :payload payload
                                          :execution-p execution :deadline (+ (genera-now) timeout))))
        (setf (gethash (getf envelope :id) (genera-session-pending session)) request
              (genera-session-queue session) (nconc (genera-session-queue session) (list request)))
        (if execution (setf (genera-session-execution session) request)
            (incf (genera-session-controls session)))
        (sb-thread:condition-broadcast (genera-session-changed session))
        request))))

(defun genera-await-request (session request)
  "Wait for this handle only; the socket owner can complete other controls."
  (loop
    (sb-thread:with-mutex ((genera-session-lock session))
      (genera-expire-request session request)
      (when (genera-request-failure request) (error (genera-request-failure request)))
      (when (genera-request-response request)
        (return-from genera-await-request (genera-request-response request)))
      ;; SBCL returns without the mutex on timeout. Re-enter the lock above
      ;; before inspecting or changing any shared request state.
      (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session)
                               :timeout (max .001 (- (genera-request-deadline request) (genera-now)))))))

(defun genera-call (session operation &rest fields)
  (genera-await-request session (apply #'genera-submit-request session operation fields)))

(defun genera-control-connection (session)
  "Explicit control retry only. Preserve execution identity; never replay it."
  (sb-thread:with-mutex ((genera-session-lock session))
    (loop while (eq (genera-session-state session) :connecting)
          do (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session))))
  (unless (eq (genera-session-state session) :ready)
    (sb-thread:join-thread (genera-session-owner session)
                          :timeout (+ .5 (genera-session-io-timeout session)) :default nil)
    (sb-thread:with-mutex ((genera-session-lock session))
      (when (or (genera-session-stop session)
                (sb-thread:thread-alive-p (genera-session-owner session)))
        (genera-session-error :closed "Genera control connection is unavailable"))
      (clrhash (genera-session-pending session))
      (setf (genera-session-controls session) 0 (genera-session-queue session) nil
            (genera-session-failure session) nil (genera-session-state session) :connecting
            (genera-session-phase session) :connect (genera-session-prefix session) :start
            (genera-session-input session) nil (genera-session-digits session) 0
            (genera-session-length session) 0 (genera-session-position session) 0
            (genera-session-owner session)
            (sb-thread:make-thread (lambda () (genera-run-session session)) :name "Genera I/O"))))
  (sb-thread:with-mutex ((genera-session-lock session))
    (loop while (eq (genera-session-state session) :connecting)
          do (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session)))
    (when (genera-session-failure session) (error (genera-session-failure session))))
  session)

(defun genera-identity-p (request id boot service)
  (let ((envelope (genera-request-envelope request)))
    (and (equal id (getf envelope :id)) (equal boot (getf envelope :boot-id))
         (equal service (getf envelope :service-id)))))

(defun genera-check-generation (session boot service)
  (unless (and (equal boot (getf (genera-session-hello session) :boot-id))
               (equal service (getf (genera-session-hello session) :service-id)))
    (genera-session-error :generation-changed "Requested generation differs; prior history is unavailable")))

(defun genera-lookup-request (session id boot service)
  (genera-control-connection session)
  (genera-check-generation session boot service)
  (let ((response (genera-call session :request-status :target-id id)) (source :ledger))
    (when (getf response :ok)
      (sb-thread:with-mutex ((genera-session-lock session))
        (let* ((work (genera-session-execution session))
               (known (or (when (and work (genera-identity-p work id boot service)) work)
                          (find-if (lambda (item) (genera-identity-p item id boot service))
                                   (genera-session-history session))))
               (result (getf response :result)))
          (cond (result
                 (if known
                     (progn
                       (when (and (genera-request-response known)
                                  (not (equal result (genera-request-response known))))
                         (genera-session-error :protocol "Ledger result conflicts with retained result"))
                       (setf (genera-request-response known) result
                             (genera-request-state known) :completed
                             (genera-request-remote-state known) (getf response :request-state))
                       (genera-release session known))
                     (genera-remember session
                       (%make-genera-request :execution-p t :state :completed :response result
                         :envelope (list :version 1 :op :eval :id id :boot-id boot :service-id service)))))
                ((and known (getf (genera-request-response known) :request-state))
                 ;; A missing remote cache entry cannot erase a known result.
                 (setf response (copy-list response) source :host
                       (getf response :request-state) (getf (genera-request-response known) :request-state)
                       (getf response :result) (genera-request-response known)))
                (t
                 (unless known
                   (when (>= (count-if (lambda (item)
                                         (and (eq (genera-request-state item) :unresolved)
                                              (not (genera-request-acknowledged item))))
                                       (genera-session-history session)) +genera-history-limit+)
                     (genera-session-error :busy "Unresolved history capacity is occupied"))
                   (setf known (%make-genera-request :execution-p t :state :unresolved
                                :envelope (list :version 1 :op :eval :id id :boot-id boot
                                                :service-id service)))
                   (if work (genera-remember session known)
                       (setf (genera-session-execution session) known)))
                 (setf (genera-request-remote-state known) (getf response :request-state))
                 (when (and (eq (getf response :request-state) :running)
                            (null (genera-session-execution session)))
                   (setf (genera-request-state known) :unresolved
                         (genera-session-execution session) known))))
          (sb-thread:condition-broadcast (genera-session-changed session)))))
    (values response source)))

(defun genera-recover-request (session id boot service expected-boot expected-service acknowledge)
  (unless acknowledge (genera-session-error :acknowledgement-required "Acknowledge the specific unknown outcome"))
  (genera-control-connection session)
  (genera-check-generation session expected-boot expected-service)
  (sb-thread:with-mutex ((genera-session-lock session))
    (let ((work (genera-session-execution session)))
      (when (and work (not (genera-identity-p work id boot service)))
        (genera-session-error :busy "Recovery identity does not match the unresolved execution"))))
  (let* ((same (and (equal boot expected-boot) (equal service expected-service)))
         (lookup (when same (genera-lookup-request session id boot service))))
    (when (and same (or (not (getf lookup :ok))
                       (not (eq (getf lookup :request-state) :unknown))))
      (genera-session-error :not-unknown "Recovery requires an unknown ledger outcome"))
    (sb-thread:with-mutex ((genera-session-lock session))
      (let ((work (genera-session-execution session)))
        (unless (and work (genera-identity-p work id boot service))
          (genera-session-error :busy "Recovery identity does not match the unresolved execution"))))
    (let ((status (genera-call session :status)))
      (unless (and (getf status :ok) (not (getf status :busy)))
        (genera-session-error :busy "Fresh status does not verify an idle Genera worker"))
      (sb-thread:with-mutex ((genera-session-lock session))
        (genera-check-generation session expected-boot expected-service)
        (let ((work (genera-session-execution session)))
          (unless (and work (genera-identity-p work id boot service)
                       (not (genera-request-response work)))
            (genera-session-error :not-unknown "Outcome changed during recovery"))
          (setf (genera-request-state work) :unknown (genera-request-acknowledged work) t
                (genera-request-remote-state work) :unknown)
          (genera-release session work))))
    (list :version 1 :id id :boot-id boot :service-id service :ok t :request-state :unknown)))
