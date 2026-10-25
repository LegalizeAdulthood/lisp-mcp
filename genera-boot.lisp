;;; -*- Mode: LISP; Syntax: Common-Lisp; Package: CL-USER -*-
;;; Save these definitions and the registered hook in a separate world.
;;; Runtime streams, locks, workers, and request history are never restored.

(defconstant +mcp-boot-source-version+ 1)
(defvar *mcp-boot-configuration* nil)
(defvar *mcp-native-boot-marker* nil)
(defvar *mcp-boot-state* :uninstalled)
(defvar *mcp-boot-phase* nil)
(defvar *mcp-boot-error* nil)

;;; --- Portable boot policy ---

(defun mcp-check-boot-sources ()
  (unless (and (boundp '+mcp-listener-source-version+)
               (= +mcp-listener-source-version+ 1)
               (fboundp 'mcp-v1-decode))
    (error "Load the matching protocol and listener sources first")))

(defun mcp-same-native-boot-p (marker)
  ;; COLD-BOOT-HISTORY gets a fresh head cons on cold boot, even when two
  ;; restores share a timestamp. The native clock pair also covers warm boot.
  (and *mcp-native-boot-marker*
       (eq (first marker) (first *mcp-native-boot-marker*))
       (equal (rest marker) (rest *mcp-native-boot-marker*))))

(defun mcp-configure-boot (protocol token world enable-network)
  (mcp-check-boot-sources)
  (mcp-check-service-configuration protocol token world)
  (setf *mcp-boot-configuration*
        (list :protocol protocol :token token :world world
              :enable-network (not (null enable-network))))
  ;; Installing in a live world must not discard an in-flight request.
  (unless *mcp-native-boot-marker*
    (setf *mcp-native-boot-marker* (mcp-native-boot-marker)))
  (setf *mcp-boot-error* nil *mcp-boot-state* :installed)
  +mcp-boot-source-version+)

(defun mcp-boot-start-1 ()
  (setf *mcp-boot-phase* :sources)
  (mcp-check-boot-sources)
  (unless *mcp-boot-configuration* (error "Install the MCP boot hook first"))
  (let ((marker (mcp-native-boot-marker)))
    (unless (mcp-same-native-boot-p marker)
      ;; Never acquire a saved bridge lock or close a saved socket. Genera
      ;; has already reset its network and process machinery at this point.
      (setf *mcp-boot-phase* :runtime)
      (mcp-reset-native-runtime)
      (setf *mcp-boot-phase* :identity)
      (mcp-initialize-boot)
      (setf *mcp-native-boot-marker* marker))
    (setf *mcp-boot-state* :starting *mcp-boot-error* nil)
    (when (and (getf *mcp-boot-configuration* :enable-network)
               (not (getf *mcp-service* :running)))
      (setf *mcp-boot-phase* :network)
      (mcp-enable-boot-network))
    (setf *mcp-boot-phase* :service)
    (let ((id (mcp-start-service
               :protocol (getf *mcp-boot-configuration* :protocol)
               :token (getf *mcp-boot-configuration* :token)
               :world (or (getf *mcp-boot-configuration* :world)
                          (mcp-detect-world)))))
      (setf *mcp-boot-state* :ready *mcp-boot-phase* :ready)
      id)))

(defun mcp-boot-start ()
  "Start once per native boot; contain failures so Genera can finish booting."
  (multiple-value-bind (success result) (mcp-call-contained #'mcp-boot-start-1)
    (if success result
        (progn
          ;; Do not retain or print arbitrary conditions: they may contain
          ;; configuration secrets or refer to circular runtime structures.
          (setf *mcp-boot-state* :failed
                *mcp-boot-error* "MCP startup failed; check sources and configuration")
          nil))))

(defun mcp-boot-status ()
  "Return bounded installation diagnostics, without tokens or runtime objects."
  (list :boot-source +mcp-boot-source-version+
        :listener-source (and (boundp '+mcp-listener-source-version+)
                              +mcp-listener-source-version+)
        :state *mcp-boot-state* :phase *mcp-boot-phase* :error *mcp-boot-error*
        :boot-id *mcp-boot-id* :service-id (getf *mcp-service* :service-id)
        :running (and (eq *mcp-boot-state* :ready)
                      (not (null (getf *mcp-service* :running))))
        :protocol (getf *mcp-boot-configuration* :protocol)
        :enable-network (getf *mcp-boot-configuration* :enable-network)))

;;; --- Genera boot adapters ---

(defun mcp-native-boot-marker ()
  (list (first si:cold-boot-history)
        time:*boot-microsecond-time-high* time:*boot-microsecond-time-low*))

(defun mcp-enable-boot-network ()
  (si:com-enable-network))

(defun mcp-reset-native-runtime ()
  (when (eq *mcp-worker* scl:*current-process*)
    (error "Native boot reset cannot run on the execution worker"))
  (sys:disable-services '(:mcp-server))
  (when (mcp-worker-live-p) (scl:process-kill *mcp-worker* t))
  (when *mcp-service* (setf (getf *mcp-service* :running) nil))
  (dolist (client *mcp-connections*) (setf (getf client :closed) t))
  (setf *mcp-service* nil *mcp-boot-id* nil *mcp-worker* nil
        *mcp-worker-busy* nil *mcp-worker-failed* nil
        *mcp-bootstrap-service* nil *mcp-connections* nil
        *mcp-current-client* nil *mcp-current-connection* nil
        *mcp-listener-installed* nil
        *mcp-state-lock* (process:make-lock "MCP state" :recursive t)
        *mcp-lifecycle-lock* (process:make-lock "MCP lifecycle" :recursive t)))

(defun mcp-install-boot-hook (&key (protocol *mcp-protocol*)
                                   (token (getf *mcp-service* :token))
                                   world enable-network)
  "Register startup for a subsequently saved world. Do not save it here."
  (mcp-configure-boot protocol token world enable-network)
  ;; Verified in SYS:SYS;LTOP and SYS:SYS;COMMAND-LOOP: WARM runs after
  ;; namespace/network initialization on cold boots and warm boots alike.
  ;; ADD-INITIALIZATION replaces the entry with this name on repeated loads.
  (si:add-initialization "MCP listener" '(mcp-boot-start) '(:warm :normal))
  +mcp-boot-source-version+)

