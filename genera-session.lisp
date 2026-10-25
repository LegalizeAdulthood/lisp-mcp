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

(defstruct genera-startup-cycle id deadline (phase :attach-first) done failure
  launch-decided launch-attempted launch-info launch-handle)

(defstruct (genera-session (:constructor %make-genera-session))
  host port token expected-world (io-timeout 5) (connect-timeout 5) owner socket hello
  runtime startup-managed startup startup-thread (startup-counter 0) (boot-timeout 300) features-changed
  reconnect supervisor (retry-min .25) (retry-max 5) (health-interval 30)
  (observe-interval .5) (failures 0) (reconnects 0) retry-at health-at observe-at
  reconcile-needed generation-change cached-at observed-at connection-error (status-stale t)
  (maintenance-lock (sb-thread:make-mutex :name "Genera reconnect"))
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
    (genera-session-error (condition) (error condition))
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
        (setf (genera-session-status-stale session) t
              (genera-session-reconcile-needed session) nil)
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
    (let ((cycle (genera-session-startup session)))
      (when (and cycle (genera-startup-cycle-done cycle) (genera-startup-cycle-failure cycle))
        (setf condition (genera-startup-cycle-failure cycle))))
    (setf (slot-value condition 'session) session)
    (setf (genera-session-failure session) condition
          (genera-session-state session) (if (genera-session-stop session) :closed :failed)
          (genera-session-phase session) :failed
          (genera-session-queue session) nil)
    (setf (genera-session-status-stale session) t)
    (when (and (not (genera-session-stop session))
               (member (genera-session-error-kind condition) '(:transport :protocol)))
      (let ((delay (min (genera-session-retry-max session)
                        (* (genera-session-retry-min session)
                           (expt 2 (min 20 (genera-session-failures session)))))))
        (incf (genera-session-failures session))
        (setf (genera-session-retry-at session) (+ (genera-now) delay))))
    (dolist (request (loop for value being the hash-values of (genera-session-pending session)
                          collect value))
      (unless (genera-request-failure request) (setf (genera-request-failure request) condition))
      (if (eq (genera-request-state request) :queued)
          (progn (setf (genera-request-state request) :not-submitted)
                 (genera-release session request))
          (setf (genera-request-state request) :unresolved)))
    (let ((work (genera-session-execution session)))
      (when (and work (eq (genera-request-state work) :unresolved))
        (setf (genera-request-remote-state work) nil
              (genera-session-reconcile-needed session) work)))
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-session-check-response (request response)
  (unless (every (lambda (key) (equal (getf request key) (getf response key)))
                 '(:id :boot-id :service-id))
    (mcp-v1-error "Response identity does not match its request"))
  (case (getf request :op)
    ((:eval :reset-listener-context) (mcp-v1-check-eval-response request response))
    (:status
     (when (getf response :ok)
       (dolist (key '(:world :package :busy :features))
         (unless (mcp-v1-present-p response key) (mcp-v1-error "Incomplete status")))))
    ((:request-status :interrupt)
     (when (getf response :ok)
       (unless (and (equal (getf request :target-id) (getf response :target-id))
                    (mcp-v1-present-p response :request-state)
                    (eq (not (null (getf response :result)))
                        (not (null (member (getf response :request-state)
                                           '(:completed :failed :interrupted))))))
         (mcp-v1-error "Incomplete or mismatched request status")))))
  (when (and (eq (getf request :op) :interrupt) (getf response :ok)
             (not (eq (getf response :request-state) :interrupted)))
    (mcp-v1-error "Interrupt success requires a terminal interrupted result"))
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
          (setf (genera-session-cached-status session) response
                (genera-session-cached-at session) (genera-now)
                (genera-session-observed-at session) (- (get-universal-time) 2208988800)
                (genera-session-status-stale session) nil
                (genera-session-failures session) 0))
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
        (when (genera-request-execution-p request)
          (setf (genera-session-status-stale session) t
                (genera-session-failures session) 0))
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
  (let* ((socket nil) (validated nil)
         (cycle (genera-session-startup session))
         (connect-bound (if (and cycle (not (genera-startup-cycle-done cycle)))
                            (max .001 (min (genera-session-connect-timeout session)
                                           (- (genera-startup-cycle-deadline cycle) (genera-now))))
                            (genera-session-connect-timeout session))))
    (handler-case
        (unwind-protect
            (progn
              (genera-with-io session
               (sb-ext:with-timeout connect-bound
               (setf socket (usocket:socket-connect
                            (genera-session-host session) (genera-session-port session)
                            :element-type '(unsigned-byte 8)
                            ;; One deadline covers DNS and connect. A second
                            ;; usocket timeout can fire during error unwinding.
                            :timeout nil))))
              (sb-thread:with-mutex ((genera-session-lock session))
                (setf (genera-session-phase session) :hello)
                (when (genera-session-stop session)
                  (genera-session-error :closed "Genera session closed while connecting")))
              (let* ((stream (usocket:socket-stream socket))
                     (hello (genera-session-hello-envelope session))
                     (greeting
                       (genera-with-io session
                        (sb-ext:with-timeout
                            (if (and cycle (not (genera-startup-cycle-done cycle)))
                                (max .001 (min connect-bound (- (genera-startup-cycle-deadline cycle) (genera-now))))
                                connect-bound)
                         (genera-write-envelope stream hello)
                         (let* ((size (read-frame-length stream))
                                (data (read-exactly stream
                                        (make-array size :element-type '(unsigned-byte 8))))
                                (text (map 'string #'code-char data)))
                           (genera-session-checked
                            (lambda ()
                              (let ((response (mcp-v1-decode text :response)))
                                (when (and (getf response :ok) (genera-session-expected-world session)
                                           (not (equal (getf response :world)
                                                       (genera-session-expected-world session))))
                                  (genera-session-error :handshake "Genera hello world mismatch"))
                                (mcp-v1-check-hello-response hello response)))))))))
                (unless (getf greeting :ok)
                  (genera-session-error :handshake "Genera rejected hello" greeting))
                (sb-thread:with-mutex ((genera-session-lock session))
                  (let ((current (genera-session-startup session)))
                    (when (and current (or (genera-startup-cycle-failure current)
                                           (and (not (genera-startup-cycle-done current))
                                                (>= (genera-now) (genera-startup-cycle-deadline current)))))
                      (genera-session-error :boot-timeout "Genera hello arrived after the startup deadline")))
                  (let ((previous (genera-session-hello session)))
                    (when (and previous
                               (not (every (lambda (key) (equal (getf previous key) (getf greeting key)))
                                           '(:boot-id :service-id))))
                      (setf (genera-session-generation-change session)
                            (list :previous-boot-id (getf previous :boot-id)
                                  :previous-service-id (getf previous :service-id)
                                  :boot-id (getf greeting :boot-id) :service-id (getf greeting :service-id)
                                  :world-state-lost (not (equal (getf previous :boot-id) (getf greeting :boot-id)))
                                  :history-lost t)))
                    (when (and previous
                               (every (lambda (key) (equal (getf previous key) (getf greeting key)))
                                      '(:boot-id :service-id))
                               (not (equal (getf previous :world) (getf greeting :world))))
                      (genera-session-error :protocol "World changed without a generation change")))
                  (setf validated t (genera-session-socket session) socket
                        (genera-session-hello session) greeting
                        (genera-session-cached-status session) greeting
                        (genera-session-cached-at session) (genera-now)
                        (genera-session-observed-at session) (- (get-universal-time) 2208988800)
                        (genera-session-status-stale session) nil
                        (genera-session-health-at session) (+ (genera-now) (genera-session-health-interval session))
                        (genera-session-observe-at session) (genera-now)
                        (genera-session-state session) :ready
                        (genera-session-phase session) :ready)
                  (sb-thread:condition-broadcast (genera-session-changed session)))
                (when (genera-session-features-changed session)
                  (funcall (genera-session-features-changed session) (getf greeting :features)))
                (loop
                  (let ((request
                          (sb-thread:with-mutex ((genera-session-lock session))
                            (when (genera-session-stop session) (return))
                            (when (genera-session-connection-error session)
                              (error (genera-session-connection-error session)))
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
          (make-condition 'genera-session-error
                          :kind (if (and socket (not validated) (genera-session-startup-managed session))
                                    :handshake :transport)
                          :format-control "Genera socket I/O timed out")))
      (error ()
        (genera-session-fail session
          (make-condition 'genera-session-error
                          :kind (if (and socket (not validated) (genera-session-startup-managed session))
                                    :handshake :transport)
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
    (when (genera-session-owner session) (ignore-errors
      (sb-thread:interrupt-thread (genera-session-owner session)
        (lambda ()
          (when (genera-session-blocking session)
            (genera-session-error :closed "Genera host I/O closed")))))))
  (when (genera-session-startup-thread session)
    (sb-thread:join-thread (genera-session-startup-thread session)
                           :timeout (+ (genera-session-connect-timeout session) 3) :default nil))
  (when (genera-session-owner session)
    (sb-thread:join-thread (genera-session-owner session)
                         :timeout (+ (max (genera-session-io-timeout session)
                                          (genera-session-connect-timeout session)) .5) :default nil))
  (when (genera-session-supervisor session)
    (sb-thread:join-thread (genera-session-supervisor session)
                          :timeout (+ (max (genera-session-io-timeout session)
                                           (genera-session-connect-timeout session)) .5) :default nil)
    (when (sb-thread:thread-alive-p (genera-session-supervisor session))
      (genera-session-error :shutdown "Genera maintenance did not stop within its I/O bound")))
  (when (and (genera-session-owner session) (sb-thread:thread-alive-p (genera-session-owner session)))
    (genera-session-error :shutdown "Genera I/O owner did not stop within its I/O bound"))
  nil)

(defun genera-begin-session (&key (host *genera-host*) (port *genera-port*) token
                                expected-world (io-timeout 5) (connect-timeout 5) reconnect
                                runtime startup-managed (boot-timeout 300) features-changed
                                (retry-min .25) (retry-max 5) (health-interval 30) (observe-interval .5))
  "Start the I/O owner without waiting for connection or hello."
  (dolist (value (list io-timeout connect-timeout boot-timeout retry-min retry-max health-interval observe-interval))
    (unless (and (realp value) (> value 0)) (error "Session timing bounds must be positive")))
  (unless (<= retry-min retry-max) (error "Reconnect delay minimum exceeds maximum"))
  (let ((session (%make-genera-session :host host :port port :token token
                                     :expected-world expected-world :io-timeout io-timeout
                                     :connect-timeout connect-timeout :reconnect reconnect
                                     :runtime runtime :startup-managed startup-managed :boot-timeout boot-timeout
                                     :features-changed features-changed
                                     :retry-min retry-min :retry-max retry-max
                                     :health-interval health-interval :observe-interval observe-interval)))
    (mcp-v1-encode (genera-session-hello-envelope session) :request)
    (if startup-managed
        (progn (setf (genera-session-state session) :disconnected) (genera-start-cycle session))
        (setf (genera-session-owner session)
              (sb-thread:make-thread (lambda () (genera-run-session session)) :name "Genera I/O")))
    (when reconnect
      (setf (genera-session-supervisor session)
            (sb-thread:make-thread (lambda () (genera-maintain-session session)) :name "Genera maintenance")))
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
    (unless (member operation '(:eval :status :request-status :interrupt :reset-listener-context))
      (genera-session-error :unsupported "Unsupported session operation"))
    (unless (member operation (getf (genera-session-hello session) :features))
      (genera-session-error :unsupported "Listener does not advertise this operation"))
    (let ((expected (getf fields :expected-generation)))
      (when expected (apply #'genera-check-generation session expected))
      (remf fields :expected-generation))
    (let* ((execution (not (null (member operation '(:eval :reset-listener-context)))))
           (idle-only (getf fields :idle-only))
           (idle-option (remf fields :idle-only))
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
      (declare (ignore ignored idle-option))
      (unless (and (realp timeout) (> timeout 0)) (error "Host timeout must be positive"))
      (when (and idle-only
                 (or (genera-session-execution session)
                     (and (getf (genera-session-cached-status session) :busy)
                          (not (genera-session-status-stale session)))))
        (genera-session-error :busy "Periodic health check requires an idle session"))
      (when (if execution (genera-session-execution session)
                (>= (genera-session-controls session) +genera-control-limit+))
        (genera-session-error :busy "Session request capacity is occupied"))
      (let ((request (%make-genera-request :envelope envelope :payload payload
                                          :execution-p execution :deadline (+ (genera-now) timeout))))
        (setf (gethash (getf envelope :id) (genera-session-pending session)) request
              (genera-session-queue session) (nconc (genera-session-queue session) (list request)))
        (if execution (setf (genera-session-execution session) request
                            (genera-session-status-stale session) t)
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

(defun genera-wait-for-connection (session)
  (loop
    (sb-thread:with-mutex ((genera-session-lock session))
      (unless (eq (genera-session-state session) :connecting) (return))
      (when (genera-session-stop session) (genera-session-error :closed "Genera host is closing"))
      (let ((cycle (genera-session-startup session)))
        (when (and cycle (not (genera-startup-cycle-done cycle))
                   (>= (genera-now) (genera-startup-cycle-deadline cycle)))
          (genera-session-error :boot-timeout "Genera startup exceeded its boot deadline")))
      (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session) :timeout .05))))

(defun genera-control-connection (session)
  "Serialize control retries with maintenance; never replay execution."
  (sb-thread:with-mutex ((genera-session-lock session))
    (let ((cycle (genera-session-startup session)))
      (when (and cycle (not (genera-startup-cycle-done cycle))
                 (not (eq sb-thread:*current-thread* (genera-session-startup-thread session))))
        (genera-session-error :not-ready "Genera startup is in progress; join it with genera_ensure_session"))))
  (sb-thread:with-mutex ((genera-session-maintenance-lock session))
  (genera-wait-for-connection session)
  (unless (eq (genera-session-state session) :ready)
    (when (genera-session-owner session)
      (sb-thread:join-thread (genera-session-owner session)
                            :timeout (+ .5 (genera-session-io-timeout session)) :default nil))
    (sb-thread:with-mutex ((genera-session-lock session))
      (when (or (genera-session-stop session)
                (and (genera-session-owner session) (sb-thread:thread-alive-p (genera-session-owner session))))
        (genera-session-error :closed "Genera control connection is unavailable"))
      (clrhash (genera-session-pending session))
      (when (genera-session-owner session) (incf (genera-session-reconnects session)))
      (setf (genera-session-controls session) 0 (genera-session-queue session) nil
            (genera-session-connection-error session) nil
            (genera-session-failure session) nil (genera-session-state session) :connecting
            (genera-session-phase session) :connect (genera-session-prefix session) :start
            (genera-session-input session) nil (genera-session-digits session) 0
            (genera-session-length session) 0 (genera-session-position session) 0
            (genera-session-owner session)
            (sb-thread:make-thread (lambda () (genera-run-session session)) :name "Genera I/O"))))
  (genera-wait-for-connection session)
  (sb-thread:with-mutex ((genera-session-lock session))
    (when (genera-session-failure session) (error (genera-session-failure session))))
  session))

(defun genera-maintain-session (session)
  "Bounded control observations and reconnects. This thread never evaluates."
  (loop
    (let ((action nil) (work nil) (owner nil))
      (sb-thread:with-mutex ((genera-session-lock session))
        (when (genera-session-stop session) (return-from genera-maintain-session nil))
        (let* ((now (genera-now)) (failure (genera-session-failure session))
               (ready (eq (genera-session-state session) :ready))
               (pending (genera-session-reconcile-needed session)))
          (setf owner (genera-session-owner session))
          (cond
            ((and (not ready) failure
                  (or (not (genera-session-startup-managed session))
                      (let ((cycle (genera-session-startup session)))
                        (and cycle (genera-startup-cycle-done cycle)
                             (not (genera-startup-cycle-failure cycle)))))
                  (member (genera-session-error-kind failure) '(:transport :protocol)))
             (setf (genera-session-phase session) :backoff)
             (when (>= now (or (genera-session-retry-at session) now)) (setf action :connect)))
            ((and ready pending (eq pending (genera-session-execution session))
                  (not (eq (genera-request-remote-state pending) :unknown))
                  (equal (getf (genera-request-envelope pending) :boot-id)
                         (getf (genera-session-hello session) :boot-id))
                  (equal (getf (genera-request-envelope pending) :service-id)
                         (getf (genera-session-hello session) :service-id))
                  (>= now (or (genera-session-observe-at session) now)))
             (setf action :lookup work pending
                   (genera-session-observe-at session) (+ now (genera-session-observe-interval session))))
            ((and ready (null (genera-session-execution session))
                  (not (and (getf (genera-session-cached-status session) :busy)
                            (not (genera-session-status-stale session))))
                  (zerop (genera-session-controls session))
                  (>= now (or (genera-session-health-at session) now)))
             (setf action :health
                   (genera-session-health-at session) (+ now (genera-session-health-interval session)))))))
      (handler-case
          (case action
            (:connect (genera-control-connection session))
            (:lookup (let ((identity (genera-request-envelope work)))
                       (genera-lookup-request session (getf identity :id) (getf identity :boot-id)
                                              (getf identity :service-id))))
            (:health
             (let ((response (genera-call session :status :idle-only t)))
               (unless (getf response :ok)
                 (genera-session-error :protocol "Genera health check was rejected")))))
        (genera-session-error (condition)
          (when (and (member action '(:health :lookup))
                     (member (genera-session-error-kind condition) '(:timeout :protocol)))
            ;; Ask the owner to fail at its next safe loop boundary.
            (sb-thread:with-mutex ((genera-session-lock session))
              (when (and (eq owner (genera-session-owner session))
                         (eq :ready (genera-session-state session)))
                (setf (genera-session-connection-error session)
                      (make-condition 'genera-session-error :kind :transport
                                      :format-control "Genera control observation failed"))))))
        (error () nil))
      ;; CONDITION-WAIT can return without its lock on timeout. Re-enter
      ;; the outer critical section before reading any session facts.
      (sb-thread:with-mutex ((genera-session-lock session))
        (unless (genera-session-stop session)
          (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session)
                                   :timeout .05))))))

(defun genera-startup-check (session cycle)
  (when (genera-session-stop session) (genera-session-error :closed "Genera host is closing"))
  (when (>= (genera-now) (genera-startup-cycle-deadline cycle))
    (genera-session-error :boot-timeout "Genera startup timed out; preserve the VLM and inspect its listener/log")))

(defun genera-startup-phase (session cycle phase)
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (genera-startup-cycle-phase cycle) phase)
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-finish-startup (session cycle &optional failure)
  ;; Publish failure before making the ticket retryable. The I/O owner's
  ;; connect/hello waits already end at this cycle's deadline; do not race
  ;; their timeout unwind with a second asynchronous interruption.
  (when failure (genera-session-fail session failure))
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (genera-startup-cycle-failure cycle) failure
          (genera-startup-cycle-phase cycle) (if failure :failed :ready)
          (genera-startup-cycle-done cycle) t)
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-startup-report-error (report)
  (let* ((check (find-if-not (lambda (row) (getf row :ok))
                             (getf (getf report :preflight) :checks)))
         (code (or (getf check :code) (getf report :code) :launch-failed))
         (message (or (getf check :message) (getf report :message)
                      (format nil "Genera startup stopped: ~A; inspect runtime identity and VLM log" code))))
    (genera-session-error code message)))

(defun genera-run-startup (session cycle)
  "One bounded attach/discover/launch cycle. Never replay execution."
  (handler-case
      (progn
        (when (and (null (genera-session-expected-world session))
                   (genera-local-autostart-p (genera-session-runtime session)))
          (setf (genera-session-expected-world session)
                (genera-runtime-world-name (genera-session-runtime session) t)))
      (loop
        (genera-startup-check session cycle)
        (when (genera-startup-cycle-launch-handle cycle)
          (let ((observation (funcall (symbol-function 'genera-launch-observation)
                                     (genera-startup-cycle-launch-handle cycle))))
            (when (member (getf observation :state) '(:exited :unknown))
              (genera-startup-report-error observation))))
        (genera-startup-phase session cycle (if (genera-startup-cycle-launch-decided cycle) :boot :attach-first))
        (handler-case
            (progn (genera-control-connection session)
                   (genera-finish-startup session cycle) (return))
          (genera-session-error (condition)
            (unless (eq (genera-session-error-kind condition) :transport) (error condition))))
        (genera-startup-check session cycle)
        (when (and (genera-local-autostart-p (genera-session-runtime session))
                   (not (genera-startup-cycle-launch-decided cycle)))
          (genera-startup-phase session cycle :preflight)
          (multiple-value-bind (report handle)
              (funcall (symbol-function 'genera-launch-vlm) (genera-session-runtime session)
                :deadline (genera-startup-cycle-deadline cycle)
                :before-launch (lambda ()
                                 (genera-startup-check session cycle)
                                 (genera-startup-phase session cycle :launch))
                :endpoint-observer
                (lambda (runtime)
                  (declare (ignore runtime))
                  (genera-startup-check session cycle)
                  (genera-startup-phase session cycle :lock)
                  (handler-case (progn (genera-control-connection session) :present)
                    (genera-session-error (condition)
                      (if (eq (genera-session-error-kind condition) :transport) :absent (error condition))))))
            (sb-thread:with-mutex ((genera-session-lock session))
              (setf (genera-startup-cycle-launch-info cycle) report
                    (genera-startup-cycle-launch-handle cycle) handle
                    (genera-startup-cycle-launch-attempted cycle) (getf report :attempted)))
            (case (getf report :action)
              ((:launched :observe)
               (setf (genera-startup-cycle-launch-decided cycle) t))
              (:blocked (unless (eq (getf report :code) :lock-busy) (genera-startup-report-error report)))
              (otherwise (genera-startup-report-error report)))))
        (genera-startup-phase session cycle (if (genera-startup-cycle-launch-decided cycle) :boot :backoff))
        (sb-thread:with-mutex ((genera-session-lock session))
          (unless (genera-session-stop session)
            (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session)
                                     :timeout (min .1 (max .001 (- (genera-startup-cycle-deadline cycle) (genera-now)))))))))
    (genera-session-error (condition) (genera-finish-startup session cycle condition))
    (error ()
      (genera-finish-startup session cycle
        (make-condition 'genera-session-error :kind :startup-failed
                        :format-control "Genera startup failed; inspect runtime permissions and VLM log")))))

(defun genera-start-cycle (session)
  "Join the active ticket, return ready, or start one explicit retry."
  (sb-thread:with-mutex ((genera-session-lock session))
    (when (genera-session-stop session) (genera-session-error :closed "Genera host is closing"))
    (let ((previous (genera-session-startup session)))
      (when (and previous (not (genera-startup-cycle-done previous)))
        (return-from genera-start-cycle previous))
      (when (eq (genera-session-state session) :ready)
        (return-from genera-start-cycle
          (or previous (make-genera-startup-cycle :id 0 :done t :phase :ready :deadline (genera-now)))))
      (let ((cycle (make-genera-startup-cycle :id (incf (genera-session-startup-counter session))
                                             :deadline (+ (genera-now) (genera-session-boot-timeout session)))))
        (setf (genera-session-startup-managed session) t (genera-session-startup session) cycle
              (genera-session-startup-thread session)
              (sb-thread:make-thread (lambda () (genera-run-startup session cycle)) :name "Genera startup"))
        cycle))))

(defun genera-await-startup (session cycle)
  (loop
    (sb-thread:with-mutex ((genera-session-lock session))
      (when (genera-startup-cycle-done cycle)
        (when (genera-startup-cycle-failure cycle) (error (genera-startup-cycle-failure cycle)))
        (return))
      (when (genera-session-stop session) (genera-session-error :closed "Genera host is closing"))
      (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session) :timeout .05)))
  cycle)

(defun genera-identity-p (request id boot service)
  (let ((envelope (genera-request-envelope request)))
    (and (equal id (getf envelope :id)) (equal boot (getf envelope :boot-id))
         (equal service (getf envelope :service-id)))))

(defun genera-check-generation (session boot service)
  (unless (and (equal boot (getf (genera-session-hello session) :boot-id))
               (equal service (getf (genera-session-hello session) :service-id)))
    (genera-session-error :generation-changed "Requested generation differs; prior history is unavailable")))

(defun genera-lookup-request (session id boot service &key timeout)
  (genera-control-connection session)
  (genera-check-generation session boot service)
  (let ((response (genera-call session :request-status :target-id id
                   :expected-generation (list boot service)
                   :host-timeout (or timeout (genera-session-io-timeout session)))) (source :ledger))
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

(defun genera-interrupt-request (session id boot service &optional (timeout 5))
  "Signal once, then require a committed interrupted result within TIMEOUT."
  (unless (and (realp timeout) (< 0 timeout) (<= timeout 5))
    (genera-session-error :invalid-request "Interrupt wait must be in (0,5] seconds"))
  (genera-control-connection session)
  (genera-check-generation session boot service)
  (let* ((end (+ (genera-now) timeout))
         (response (genera-call session :interrupt :target-id id :host-timeout timeout
                      :expected-generation (list boot service))))
    (loop
      (cond ((and (getf response :ok) (getf response :result)
                  (eq (getf response :request-state) :interrupted))
             (return response))
            ((or (eq (getf response :interrupt-state) :refused)
                 (and (not (getf response :ok))
                      (not (eq (getf (getf response :error) :code) :interrupt-pending))))
             (genera-session-error (or (getf (getf response :error) :code) :interrupt-refused)
                                   "Target interruption was refused" response))
            ((getf response :result)
             (genera-session-error :request-not-active "Target finished without a confirmed interruption" response)))
      (let ((remaining (- end (genera-now))))
        (when (<= remaining .01)
          (genera-session-error :interrupt-pending
            "No terminal interruption recorded; work may continue or require debugger recovery" response))
        (sleep (min .02 (/ remaining 2)))
        (setf response (genera-lookup-request session id boot service
                                             :timeout (max .001 (- end (genera-now)))))))))

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
